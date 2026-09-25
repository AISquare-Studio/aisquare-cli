"""``aisquare captain "text"``: deliver to the captain, wait for its turn to end, read its reply.

Contract (T2, seq 13121, item 5): an absent captain is started with the text as
its first prompt; a waiting one gets it typed; a BUSY one is waited for and then
typed — never a board note, which nothing would prompt the captain to read. The
reply is the captain's last assistant text of the answering turn, read from its
transcript once its session reads ``waiting`` past the moment the text went in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import transcripts
from aisquare.models import FleetAgent, FleetAgentState, FleetAgentStatus, TeamSession
from aisquare.services import fleet
from aisquare.services.captain import brain
from aisquare.services.captain import state as captain_state

# --- the transcript's reply ---------------------------------------------------------------


def _write_transcript(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


def _user(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": text}]},
    }


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def test_the_reply_is_the_assistant_text_after_the_last_prompt(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _user("an older question"),
            _assistant(_text("an older answer")),
            _user("what is up"),
            _assistant(_text("Checking."), {"type": "tool_use", "name": "mcp__captain__attention"}),
            _tool_result('{"items": []}'),
            _assistant(_text("Nothing needs you right now.")),
        ],
    )
    assert transcripts.last_reply(path) == "Checking.\nNothing needs you right now."


def test_no_reply_yet_or_no_file_reads_as_none(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_assistant(_text("old")), _user("new ask")])
    assert transcripts.last_reply(path) is None
    assert transcripts.last_reply(tmp_path / "missing.jsonl") is None


# --- say ------------------------------------------------------------------------------------


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime(2026, 9, 25, 10, 0, tzinfo=UTC))
    slept: float = 0.0

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += timedelta(seconds=seconds)


@dataclass
class Captain:
    """The captain as ``say`` sees it: a row, a pane, a board session, a transcript."""

    tmp: Path
    state: FleetAgentState = "waiting"
    turn_ends_after: float | None = 3.0
    """Seconds after the text goes in until the Stop hook marks the session waiting."""
    busy_for: float = 0.0
    reply: str = "Nothing needs you right now."
    typed: list[tuple[str, str]] = field(default_factory=list)
    started: list[str | None] = field(default_factory=list)
    told: list[str] = field(default_factory=list)
    typed_at: datetime | None = None
    row: FleetAgent | None = None
    session: TeamSession | None = None


@pytest.fixture
def captain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Captain, Clock]:
    clock = Clock()
    fake = Captain(tmp=tmp_path)
    transcript = tmp_path / "captain.jsonl"
    home = captain_state.home_project()
    session_id = "captain-session-1"

    def make_row() -> FleetAgent:
        return FleetAgent(
            id="agt_captain",
            project_id=home.id,
            label="captain",
            role="captain",
            pane_id="%7",
            session_id=session_id,
            cwd=tmp_path,
            created_at=clock.now,
        )

    def board_session(state: str, seen: datetime) -> None:
        # Held here, not written through the store: touch_session stamps the REAL clock,
        # and "a previous turn's waiting" must be told apart on the fake one.
        fake.session = TeamSession(
            id=session_id, project_id=home.id, role="captain", started_at=seen,
            last_seen_at=seen, state=state, transcript_path=str(transcript),
        )  # fmt: skip

    def status_now() -> FleetAgentState:
        if fake.busy_for > 0:
            return "working"
        return fake.state

    class Pane:
        def paste(self, pane_id: str, text: str) -> None:
            fake.typed.append(("paste", text))
            fake.typed_at = clock.now
            _write_transcript(transcript, [_user(text)])

        def send_keys(self, pane_id: str, *keys: str) -> None:
            fake.typed.append(("keys", " ".join(keys)))

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        fake.busy_for = max(0.0, fake.busy_for - seconds)
        if (
            fake.typed_at is not None
            and fake.turn_ends_after is not None
            and clock.now >= fake.typed_at + timedelta(seconds=fake.turn_ends_after)
        ):
            _write_transcript(transcript, [_user("x"), _assistant(_text(fake.reply))])
            board_session("waiting", clock.now)

    def start(prompt: str | None = None, *, size: Any = None) -> fleet.SpawnReceipt:
        fake.started.append(prompt)
        fake.row = make_row()
        fake.typed_at = clock.now
        if prompt is not None:
            _write_transcript(transcript, [_user(prompt)])
        board_session("working", clock.now)
        return fleet.SpawnReceipt(agent=fake.row, asked_label="captain", tmux_session="asq-x")

    def tell(*args: Any, **kwargs: Any) -> fleet.TellResult:
        fake.told.append(str(args))
        return fleet.TellResult(False, "filed as a board note")

    monkeypatch.setattr(brain, "_now", clock)
    monkeypatch.setattr(brain, "_sleep", sleep)
    monkeypatch.setattr(brain, "start", start)
    monkeypatch.setattr(brain, "find", lambda: fake.row)
    monkeypatch.setattr(brain, "_session_of", lambda agent: fake.session)
    monkeypatch.setattr(fleet, "tell", tell)
    monkeypatch.setattr(
        fleet, "status_of", lambda agent: FleetAgentStatus(agent=agent, state=status_now())
    )
    monkeypatch.setattr(fleet, "server_for", lambda socket, config=None: Pane())
    monkeypatch.setattr(fleet, "pane_is_the_agent", lambda srv, pane_id: True)
    fake_live = make_row()
    fake.row = None

    def present() -> None:
        fake.row = fake_live
        board_session("waiting", clock.now - timedelta(minutes=5))

    fake.present = present  # type: ignore[attr-defined]
    return fake, clock


def test_an_absent_captain_is_started_with_the_text_as_its_first_prompt(
    captain: tuple[Captain, Clock],
) -> None:
    fake, _ = captain
    reply = brain.say("what is up", timeout=60)
    assert fake.started == ["what is up"]
    assert fake.typed == [], "the spawn types its first prompt; say types nothing more"
    assert reply.text == "Nothing needs you right now."


def test_a_waiting_captain_gets_the_text_typed_and_its_reply_is_returned(
    captain: tuple[Captain, Clock],
) -> None:
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    reply = brain.say("what is up", timeout=60)
    assert fake.typed == [("paste", "what is up"), ("keys", "Enter")]
    assert fake.started == [] and fake.told == []
    assert reply.text == "Nothing needs you right now."


def test_a_busy_captain_is_waited_for_never_sent_a_board_note(
    captain: tuple[Captain, Clock],
) -> None:
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.busy_for = 20.0
    reply = brain.say("what is up", timeout=120)
    assert fake.told == [], "never a board note the captain is not prompted to read"
    assert fake.typed[0] == ("paste", "what is up")
    assert clock.slept >= 20.0, "it waited for the turn to end before typing"
    assert reply.text == "Nothing needs you right now."


def test_a_captain_that_does_not_answer_in_time_is_said(
    captain: tuple[Captain, Clock],
) -> None:
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.turn_ends_after = None  # the turn never ends
    with pytest.raises(brain.NoReply, match="did not answer within 30s"):
        brain.say("what is up", timeout=30)


def test_a_previous_turns_waiting_is_not_taken_for_the_answer(
    captain: tuple[Captain, Clock],
) -> None:
    """The session read ``waiting`` BEFORE the text went in: that is the last turn's end."""
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.turn_ends_after = 10.0
    reply = brain.say("what is up", timeout=60)
    assert clock.slept >= 10.0
    assert reply.text == "Nothing needs you right now."


# --- the CLI ----------------------------------------------------------------------------------


@dataclass
class Said:
    texts: list[str] = field(default_factory=list)


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> Said:
    record = Said()

    def fake_say(text: str, *, timeout: float = 180.0) -> brain.Reply:
        record.texts.append(text)
        if text == "silence":
            raise brain.NoReply(
                "the captain did not answer within 5s — its answer will be in its pane"
            )
        return brain.Reply(
            text=f"reply to: {text}", ended_at=datetime(2026, 9, 25, 10, 5, tzinfo=UTC)
        )

    monkeypatch.setattr(brain, "say", fake_say)
    return record


def test_captain_text_is_said_and_the_reply_printed(runner: CliRunner, said: Said) -> None:
    result = runner.invoke(app, ["captain", "what is up"])
    assert result.exit_code == 0, result.output
    assert said.texts == ["what is up"]
    assert "reply to: what is up" in result.output


def test_captain_say_takes_a_message_that_starts_with_a_subcommands_name(
    runner: CliRunner, said: Said
) -> None:
    result = runner.invoke(app, ["captain", "say", "serve", "the", "owner"])
    assert result.exit_code == 0, result.output
    assert said.texts == ["serve the owner"]


def test_captain_serve_is_still_a_subcommand(runner: CliRunner, said: Said) -> None:
    result = runner.invoke(app, ["captain", "serve"])
    assert result.exit_code == 2, "serve's own --stdio usage error, not a message"
    assert said.texts == []


def test_captain_chat_says_each_line_until_eof(runner: CliRunner, said: Said) -> None:
    result = runner.invoke(app, ["captain", "chat"], input="what is up\n\nnext\n")
    assert result.exit_code == 0, result.output
    assert said.texts == ["what is up", "next"], "a blank line is skipped, EOF ends"
    assert "reply to: next" in result.output


def test_bare_captain_off_a_terminal_starts_it_and_says_where(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str | None] = []
    row = FleetAgent(
        id="agt_c", project_id="prj_home", label="captain", role="captain",
        pane_id="%1", cwd=Path("/tmp"), created_at=datetime.now(tz=UTC),
    )  # fmt: skip

    def start(prompt: str | None = None, *, size: Any = None) -> fleet.SpawnReceipt:
        started.append(prompt)
        return fleet.SpawnReceipt(agent=row, asked_label="captain", tmux_session="asq-home-x")

    monkeypatch.setattr(brain, "find", lambda: None)
    monkeypatch.setattr(brain, "start", start)
    result = runner.invoke(app, ["captain"])
    assert result.exit_code == 0, result.output
    assert started == [None]
    assert "captain" in result.output and "asq-home-x" in result.output


def test_bare_captain_with_a_live_captain_off_a_terminal_says_where_it_is(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = FleetAgent(
        id="agt_c", project_id="prj_home", label="captain", role="captain",
        pane_id="%1", cwd=Path("/tmp"), created_at=datetime.now(tz=UTC),
    )  # fmt: skip
    monkeypatch.setattr(brain, "find", lambda: row)
    monkeypatch.setattr(brain, "start", lambda *a, **k: pytest.fail("must not spawn a second"))
    result = runner.invoke(app, ["captain"])
    assert result.exit_code == 0, result.output
    assert "already running" in result.output


def test_captain_text_json_is_one_object_with_the_reply_its_turns_end_and_no_timeout(
    runner: CliRunner, said: Said
) -> None:
    """Plan section 2's --json interface (manager's rider at 13123)."""
    result = runner.invoke(app, ["--json", "captain", "what is up"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "reply": "reply to: what is up",
        "ended_at": "2026-09-25T10:05:00+00:00",
        "timed_out": False,
    }


def test_captain_text_json_says_a_timeout_in_the_same_object(runner: CliRunner, said: Said) -> None:
    result = runner.invoke(app, ["--json", "captain", "silence"])
    assert result.exit_code == 1
    body = json.loads(result.stdout)
    assert (body["reply"], body["ended_at"], body["timed_out"]) == (None, None, True)
    assert body["said"].startswith("the captain did not answer within 5s")


def test_the_reply_carries_the_answering_turns_end(captain: tuple[Captain, Clock]) -> None:
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    reply = brain.say("what is up", timeout=60)
    assert fake.session is not None
    assert reply.ended_at == fake.session.last_seen_at
