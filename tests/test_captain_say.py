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
from aisquare.core.tmux import TmuxError
from aisquare.models import FleetAgent, FleetAgentState, FleetAgentStatus, TeamSession
from aisquare.services import fleet
from aisquare.services.captain import brain
from aisquare.services.captain import state as captain_state

# --- the transcript's reply ---------------------------------------------------------------


def _write_transcript(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


def _stamp(at: datetime) -> str:
    """Claude Code's own shape: UTC, milliseconds, a ``Z``."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _user(text: str, at: datetime | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": text}}
    if at is not None:
        entry["timestamp"] = _stamp(at)
    return entry


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


def test_an_unanswered_prompt_no_prompt_or_no_file_read_as_none(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_assistant(_text("old")), _user("new ask")])
    assert transcripts.last_reply(path) is None, "the prompt is there; nothing answers it yet"
    assert transcripts.last_reply(_write_transcript(tmp_path / "u.jsonl", [])) is None
    assert transcripts.last_reply(tmp_path / "missing.jsonl") is None


def test_an_answer_without_text_reads_as_empty(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_user("do it"), _assistant({"type": "tool_use", "name": "mcp__captain__tell"})],
    )
    assert transcripts.last_reply(path) == ""


def test_a_prompt_from_before_since_is_not_the_one_answered(tmp_path: Path) -> None:
    """``since`` is the moment the text went in: an older prompt's answer is not its reply."""
    typed = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_user("an older question", typed - timedelta(seconds=30)), _assistant(_text("old"))],
    )
    assert transcripts.last_reply(path, since=typed) is None
    _write_transcript(
        path,
        [
            _user("an older question", typed - timedelta(seconds=30)),
            _assistant(_text("old")),
            _user("what is up", typed + timedelta(milliseconds=400)),
            _assistant(_text("new")),
        ],
    )
    assert transcripts.last_reply(path, since=typed) == "new"


def test_a_meta_entry_is_not_a_prompt(tmp_path: Path) -> None:
    """Claude Code writes command caveats and hook context as ``user`` text marked isMeta."""
    meta = {"type": "user", "isMeta": True, "message": {"content": "<local-command-caveat>…"}}
    path = _write_transcript(
        tmp_path / "t.jsonl", [_user("what is up"), _assistant(_text("the answer")), meta]
    )
    assert transcripts.last_reply(path) == "the answer"


def test_a_turn_larger_than_the_tail_still_yields_its_reply(tmp_path: Path) -> None:
    """A tool result of several hundred KB between the prompt and the answer."""
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _user("read every pane"),
            _assistant(_text("Reading."), {"type": "tool_use", "name": "mcp__captain__read_pane"}),
            _tool_result("x" * 600_000),
            _assistant(_text("All quiet.")),
        ],
    )
    assert transcripts.last_reply(path) == "Reading.\nAll quiet."


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


from tests.captain_screens import (  # noqa: E402
    BOX_WITH_ESC_FOOTER,
    INPUT_BOX,
    REAL_IDLE_AFTER_STOP,
    REAL_TRUST,
    REAL_WORKING,
)


def _pane(*transcript: str, box: bool = True) -> list[str]:
    """A pane: the conversation, then (unless a dialog replaced it) the input box."""
    return [*transcript, *(INPUT_BOX if box else [])]


@dataclass
class Captain:
    """The captain as ``say`` sees it: a row, a pane, a board session, a transcript."""

    tmp: Path
    state: FleetAgentState = "waiting"
    turn_ends_after: float | None = 3.0
    """Seconds after the text goes in until the Stop hook marks the session waiting."""
    busy_for: float = 0.0
    busy_screen: list[str] | None = None
    """What the pane shows while ``busy_for`` runs, when set: a real busy Claude Code keeps
    its box drawn with its turn above it (runner2's REAL_WORKING), and since T2b a drawn,
    idle box is ready even while the fleet reads working."""
    reply: str = "Nothing needs you right now."
    typed: list[tuple[str, str]] = field(default_factory=list)
    started: list[str | None] = field(default_factory=list)
    told: list[str] = field(default_factory=list)
    typed_at: datetime | None = None
    typed_text: str = ""
    row: FleetAgent | None = None
    session: TeamSession | None = None
    prompt_typed: bool = True
    notes: list[str] = field(default_factory=list)
    screen: list[str] = field(default_factory=lambda: list(INPUT_BOX))
    """What the captain's pane shows, escapes already stripped — Claude Code's input box by
    default, as a waiting pane ends."""
    paste_fails: bool = False
    dies_after: float | None = None
    """Seconds after the text goes in until the captain's pane dies."""
    limited_after: float | None = None
    """Seconds after the text goes in until the captain parks on its usage limit."""


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
        if (
            fake.dies_after is not None
            and fake.typed_at is not None
            and clock.now >= fake.typed_at + timedelta(seconds=fake.dies_after)
        ):
            return "exited"
        if (
            fake.limited_after is not None
            and fake.typed_at is not None
            and clock.now >= fake.typed_at + timedelta(seconds=fake.limited_after)
        ):
            return "limited"
        if fake.busy_for > 0:
            return "working"
        return fake.state

    class Pane:
        def paste(self, pane_id: str, text: str) -> None:
            if fake.paste_fails:
                raise TmuxError("tmux paste-buffer failed: no server running")
            fake.typed.append(("paste", text))
            fake.typed_at, fake.typed_text = clock.now, text
            _write_transcript(transcript, [_user(text, clock.now)])

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
            _write_transcript(
                transcript,
                [_user(fake.typed_text, fake.typed_at), _assistant(_text(fake.reply))],
            )
            board_session("waiting", clock.now)

    def start(prompt: str | None = None, *, size: Any = None) -> fleet.SpawnReceipt:
        fake.started.append(prompt)
        fake.row = make_row()
        board_session("waiting", clock.now)  # its SessionStart: at its prompt
        clock.sleep(2.0)  # the window comes up, then the fleet types the prompt
        if prompt is not None and fake.prompt_typed:
            fake.typed_at, fake.typed_text = clock.now, prompt
            _write_transcript(transcript, [_user(prompt, clock.now)])
            board_session("working", clock.now)
        return fleet.SpawnReceipt(
            agent=fake.row,
            asked_label="captain",
            tmux_session="asq-x",
            notes=list(fake.notes),
            prompt_typed=prompt is not None and fake.prompt_typed,
        )

    def tell(*args: Any, **kwargs: Any) -> fleet.TellResult:
        fake.told.append(str(args))
        return fleet.TellResult(False, "filed as a board note")

    monkeypatch.setattr(brain, "_now", clock)
    monkeypatch.setattr(brain, "_sleep", sleep)
    monkeypatch.setattr(brain, "start", start)
    monkeypatch.setattr(brain, "find", lambda: fake.row)
    monkeypatch.setattr(brain, "_bound", lambda agent: (fake.row, fake.session))
    monkeypatch.setattr(
        brain,
        "_pane_text",
        lambda agent, srv: list(
            fake.busy_screen if fake.busy_for > 0 and fake.busy_screen is not None else fake.screen
        ),
    )
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


def test_an_absent_captain_is_started_bare_and_the_text_typed_once_it_is_at_its_prompt(
    captain: tuple[Captain, Clock],
) -> None:
    """13227: nothing types into the captain without reading its pane first, and the fleet's
    first-prompt typing cannot read a pane — so the captain starts bare, and the text goes in
    through the same guarded path once its prompt shows."""
    fake, _ = captain
    reply = brain.say("what is up", timeout=60)
    assert fake.started == [None], "started with no first prompt"
    assert fake.typed == [("paste", "what is up"), ("keys", "Enter")]
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


def test_a_captain_idle_at_a_box_drawn_from_the_first_read_is_typed_into_at_once(
    captain: tuple[Captain, Clock],
) -> None:
    """13294: the settle is for a new box. Before an idle captain it cost every voice turn 2 s."""
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    asked_at = clock.now
    brain.say("what is up", timeout=60)
    assert fake.typed_at == asked_at, "typed at once, no settle"


def test_a_box_first_seen_during_the_say_gets_its_one_settle(
    captain: tuple[Captain, Clock],
) -> None:
    """A running captain whose box is not drawn yet (a redraw, a /clear) settles once it is."""
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = []
    drawn_at: list[datetime] = []
    real_sleep = brain._sleep

    def draw_after_two_polls(seconds: float) -> None:
        real_sleep(seconds)
        if clock.slept >= 2.0 and not fake.screen:
            fake.screen = list(INPUT_BOX)
            drawn_at.append(clock.now)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(brain, "_sleep", draw_after_two_polls)
        brain.say("what is up", timeout=60)
    assert fake.started == [] and fake.typed_at is not None and drawn_at
    assert fake.typed_at - drawn_at[0] >= timedelta(seconds=brain.TYPE_SETTLE_S), "one settle"


def test_a_waiting_captain_whose_box_footer_names_esc_is_typed_into_not_refused(
    captain: tuple[Captain, Clock],
) -> None:
    """coderp's minor: with the box drawn, nothing is a dialog, whatever its footer says. The
    box check ignored reads the footer after the last rule as a dialog's."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = list(BOX_WITH_ESC_FOOTER)
    brain.say("what is up", timeout=60)
    assert fake.typed == [("paste", "what is up"), ("keys", "Enter")]


def test_a_busy_captain_is_waited_for_never_sent_a_board_note(
    captain: tuple[Captain, Clock],
) -> None:
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.busy_for = 20.0
    fake.busy_screen = list(REAL_WORKING)  # its turn on screen, as a real one shows it
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


def test_a_turn_that_ends_without_text_is_no_reply_text_never_a_placeholder(
    captain: tuple[Captain, Clock],
) -> None:
    """The captain answered with tools alone. ``Reply.text`` is ``None``: a placeholder in
    its place reads as the captain's words under ``--json``, and T3's page would speak it."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.reply = ""
    reply = brain.say("stop coder-2", timeout=60)
    assert reply.text is None
    assert reply.ended_at is not None


def test_a_started_captain_that_never_reaches_its_prompt_is_said_at_the_deadline(
    captain: tuple[Captain, Clock],
) -> None:
    fake, _ = captain
    fake.busy_for = 10_000.0  # up, but never at its prompt
    fake.busy_screen = list(REAL_WORKING)  # its turn on screen, as a real one shows it
    with pytest.raises(
        brain.NoReply, match="stayed working for 30s, so nothing was typed"
    ) as caught:
        brain.say("what is up", timeout=30)
    assert caught.value.timed_out is True
    assert fake.typed == []
    assert fake.started == [None], "started bare (13227)"


TRUST_DIALOG = [
    "Quick safety check: Is this a project you created or one you trust?",
    "❯ No, exit",  # noqa: RUF001 — Claude Code's own cursor
    "  Yes, I trust this folder",
    "Enter to confirm · Esc to cancel",
]


@pytest.mark.parametrize(
    "quoted",
    [
        ["The prompt reads:", "  ❯ 1. Yes", "  2. No, and tell Claude what to do differently"],  # noqa: RUF001
        ["coder-1 is stuck on (Enter to confirm · Esc to cancel); say yes to unblock it"],
        ["you said: choose Yes, I trust this folder"],
        ["1. approve the deploy", "2. merge #218"],
    ],
    ids=["a quoted chooser", "a quoted footer", "an echoed trust line", "a plain list"],
)  # fmt: skip
def test_dialog_words_in_the_conversation_above_a_drawn_input_box_are_not_a_dialog(
    captain: tuple[Captain, Clock], quoted: list[str]
) -> None:
    """coderp's M1 (comment 5833887964): reporting a stuck prompt IS the captain's job, so its
    reply quotes chooser lines and footers, and the owner's echoed message can hold the trust
    sentence. A dialog is read by the pane's STRUCTURE — it replaces the input box — never by
    words anywhere in the last lines; with the box drawn, say types."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = _pane(*quoted)
    assert brain.say("what is up", timeout=60).text == "Nothing needs you right now."
    assert fake.typed[0] == ("paste", "what is up")


def test_the_rating_survey_just_above_a_drawn_input_box_is_still_a_dialog(
    captain: tuple[Captain, Clock],
) -> None:
    """The one dialog that does not replace the box (coderp's M1 fix, its exception): the
    session-rating survey sits in the line or two above it. It is read there, and only there."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = _pane(
        "How is Claude doing this session? (optional)", "1: Bad  2: Fine  3: Good  0: Dismiss"
    )
    with pytest.raises(brain.NoReply, match="the session-rating prompt") as caught:
        brain.say("what is up", timeout=60)
    assert caught.value.timed_out is False and fake.typed == []
    fake.screen = _pane("Earlier I asked: How is Claude doing this session?", "The reply.", "More.")
    assert brain.say("what is up", timeout=60).text == "Nothing needs you right now."


def test_a_busy_captain_with_dialog_words_on_screen_is_waited_out_not_refused(
    captain: tuple[Captain, Clock],
) -> None:
    """M1's other half: the check ran before the state check and refused a busy captain at 0 s.
    A working pane has no input box and no dialog — it is waited out, then typed into."""
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.busy_for = 6.0
    fake.screen = ["Reading coder-1's pane: ❯ 1. Yes / Esc to cancel", "· Thinking…"]  # noqa: RUF001

    real_sleep = brain._sleep

    def settle_and_draw(seconds: float) -> None:
        real_sleep(seconds)
        if fake.busy_for <= 0:
            fake.screen = _pane("coder-1 was stuck on a chooser; I said yes.")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(brain, "_sleep", settle_and_draw)
        reply = brain.say("what is up", timeout=60)
    assert clock.slept >= 6.0, "waited out, not refused at 0 s"
    assert reply.text == "Nothing needs you right now."


def test_a_pane_that_cannot_be_read_is_said_not_raised_and_a_lost_captain_wins(
    captain: tuple[Captain, Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """coderp's M2: the pane read ran first and unguarded, so a window stopped mid-wait crashed
    say with a raw TmuxError before status_of could say lost."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]

    def unreadable(agent: Any, srv: Any) -> list[str]:
        raise TmuxError("can't find pane: %7")

    monkeypatch.setattr(brain, "_pane_text", unreadable)
    with pytest.raises(brain.NoReply, match="could not read the captain's pane") as caught:
        brain.say("what is up", timeout=60)
    assert caught.value.timed_out is False and fake.typed == []
    fake.dies_after = 0.0
    fake.typed_at = brain._now()
    with pytest.raises(brain.NoReply, match="the captain is exited") as gone:
        brain.say("what is up", timeout=60)
    assert gone.value.timed_out is False


def test_a_fresh_captain_is_typed_into_only_once_its_prompt_is_drawn_and_after_one_settle(
    captain: tuple[Captain, Clock],
) -> None:
    """coderp's M3: a blank pane (claude exec'd, nothing drawn yet) passed the guard and the text
    went in before bracketed paste was on. Positive evidence first — the input box drawn —
    then one settle, as the fleet's own first-prompt typing settles."""
    fake, clock = captain
    fake.screen = []  # the window is up, nothing is drawn yet

    real_start = brain.start

    def start_blank(prompt: str | None = None, *, size: Any = None) -> fleet.SpawnReceipt:
        receipt = real_start(prompt, size=size)
        fake.screen = []
        return receipt

    drawn_at: list[datetime] = []
    real_sleep = brain._sleep

    def draw_after_three_polls(seconds: float) -> None:
        real_sleep(seconds)
        if clock.slept >= 3.0 and not fake.screen:
            fake.screen = list(INPUT_BOX)
            drawn_at.append(clock.now)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(brain, "start", start_blank)
        mp.setattr(brain, "_sleep", draw_after_three_polls)
        reply = brain.say("what is up", timeout=60)
    assert reply.text == "Nothing needs you right now."
    assert fake.started == [None], "started bare (13227)"
    assert fake.typed_at is not None and drawn_at
    assert fake.typed_at - drawn_at[0] >= timedelta(seconds=brain.TYPE_SETTLE_S), "one settle"


def test_say_never_types_into_the_trust_dialog_and_says_how_to_answer_it(
    captain: tuple[Captain, Clock],
) -> None:
    """13227: a fresh captain parks at Claude Code's trust dialog; an Enter typed there picks
    the highlighted "No, exit". Read first, refuse at once, name the one thing to do."""
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = TRUST_DIALOG
    with pytest.raises(brain.NoReply, match="trust its folder") as caught:
        brain.say("what is up", timeout=60)
    message = str(caught.value)
    assert str(brain.brain_dir()) in message
    assert "run `aisquare captain` and choose Yes, I trust this folder (once)" in message
    assert caught.value.timed_out is False
    assert fake.typed == [], "nothing was typed into the dialog"
    assert clock.slept == 0.0, "said at once"


def test_a_fresh_captain_parked_at_the_dialog_is_said_not_waited_out(
    captain: tuple[Captain, Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, clock = captain
    real_start = brain.start

    def start_at_the_dialog(prompt: str | None = None, *, size: Any = None) -> fleet.SpawnReceipt:
        receipt = real_start(prompt, size=size)
        fake.screen = TRUST_DIALOG
        return receipt

    monkeypatch.setattr(brain, "start", start_at_the_dialog)
    with pytest.raises(brain.NoReply, match="trust its folder") as caught:
        brain.say("what is up", timeout=60)
    assert caught.value.timed_out is False and fake.typed == []
    assert fake.started == [None], "started bare: the text is never its first prompt (13227)"
    assert clock.slept < 5, "not the whole timeout"


@pytest.mark.parametrize(
    ("screen", "showing"),
    [
        (["How is Claude doing this session? (optional)", "1: Bad  2: Fine  3: Good  0: Dismiss"],
         "the session-rating prompt"),
        (
            ["Do you want to proceed?", "❯ 1. Yes", "  2. No", " Esc to cancel"],  # noqa: RUF001
            "a numbered choice",
        ),
        (
            ["Select a model", "Enter to confirm · Esc to cancel"],
            "a dialog waiting for Enter or Esc",
        ),
    ],
)  # fmt: skip
@pytest.mark.parametrize("door", ["say", "send"])
def test_say_never_types_into_a_dialog_and_names_what_is_showing(
    captain: tuple[Captain, Clock], screen: list[str], showing: str, door: str
) -> None:
    """Both doors into the captain refuse the same screens in the same words (13325)."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = screen
    with pytest.raises(brain.NoReply, match=showing) as caught:
        if door == "say":
            brain.say("what is up", timeout=60)
        else:
            brain.send("what is up", timeout=60)
    assert "`aisquare captain` attaches" in str(caught.value)
    assert caught.value.timed_out is False and fake.typed == []


def test_a_highlighted_list_mid_turn_without_a_dialog_footer_is_not_a_dialog(
    captain: tuple[Captain, Clock],
) -> None:
    """One reader with T1b (13278): a chooser is numbered options WITH the Esc/Enter footer.
    Output that happens to highlight a line mid-turn is not one, so say does not refuse it."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = [
        "Here are the options:",
        " ❯ 1. Yes",  # noqa: RUF001
        "   2. No",
        "✻ Thinking… (esc to interrupt)",
    ]
    fake.busy_for = 2.0

    real_sleep = brain._sleep

    def draw(seconds: float) -> None:
        real_sleep(seconds)
        if fake.busy_for <= 0:
            fake.screen = list(INPUT_BOX)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(brain, "_sleep", draw)
        assert brain.say("what is up", timeout=60).text == "Nothing needs you right now."


def test_a_numbered_list_in_the_captains_own_words_is_not_a_dialog(
    captain: tuple[Captain, Clock],
) -> None:
    """The captain answers in lists; a "1." in its reply must not read as a menu."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = _pane("Two things need you:", "1. approve the deploy", "2. merge #218")
    assert brain.say("what is up", timeout=60).text == "Nothing needs you right now."
    assert fake.typed[0] == ("paste", "what is up")


def test_a_captain_that_dies_before_answering_is_said_at_once(
    captain: tuple[Captain, Clock],
) -> None:
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.turn_ends_after = None
    fake.dies_after = 4.0
    with pytest.raises(brain.NoReply, match="exited") as caught:
        brain.say("what is up", timeout=120)
    assert caught.value.timed_out is False
    assert clock.slept < 10, "not the whole timeout"


def test_a_captain_parked_on_its_usage_limit_is_said_at_once(
    captain: tuple[Captain, Clock],
) -> None:
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.turn_ends_after = None
    fake.limited_after = 3.0
    with pytest.raises(brain.NoReply, match="usage limit") as caught:
        brain.say("what is up", timeout=120)
    assert caught.value.timed_out is False
    assert clock.slept < 10


def test_a_waiting_row_without_the_answering_prompt_is_not_the_answer(
    captain: tuple[Captain, Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook that bumps ``last_seen_at`` on a ``waiting`` row — a SessionEnd, a quiet
    notification — is not the turn that answered: the transcript must hold the prompt
    typed at or after the text went in."""
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.turn_ends_after = None

    real_sleep = brain._sleep

    def bump(seconds: float) -> None:
        real_sleep(seconds)
        assert fake.session is not None
        fake.session = fake.session.model_copy(update={"last_seen_at": clock.now})

    monkeypatch.setattr(brain, "_sleep", bump)
    _write_transcript(Path(str(fake.session.transcript_path)), [])  # type: ignore[union-attr]
    with pytest.raises(brain.NoReply, match="did not answer within 20s"):
        brain.say("what is up", timeout=20)


def test_an_ended_session_is_never_taken_for_the_answer(
    captain: tuple[Captain, Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.turn_ends_after = 2.0
    real_sleep = brain._sleep

    def end_it(seconds: float) -> None:
        real_sleep(seconds)
        if fake.session is not None and fake.session.state == "waiting" and fake.typed_at:
            fake.session = fake.session.model_copy(update={"ended_at": clock.now})

    monkeypatch.setattr(brain, "_sleep", end_it)
    with pytest.raises(brain.NoReply, match="did not answer within 20s"):
        brain.say("what is up", timeout=20)


def test_tmux_failing_to_type_is_said_not_raised(captain: tuple[Captain, Clock]) -> None:
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.paste_fails = True
    with pytest.raises(brain.NoReply, match="could not type") as caught:
        brain.say("what is up", timeout=60)
    assert caught.value.timed_out is False


def test_one_message_reaches_the_captain_at_a_time(captain: tuple[Captain, Clock]) -> None:
    """Two says into one waiting captain would both type and both read the same reply."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    held = brain._one_at_a_time(brain._now() + timedelta(seconds=5), 5.0)
    with held, pytest.raises(brain.NoReply, match="another message to the captain") as caught:
        brain.say("what is up", timeout=5)
    assert caught.value.timed_out is True
    assert fake.typed == []
    assert brain.say("what is up", timeout=60).text == "Nothing needs you right now."


# --- send: the one guarded door, without a reply wait (13325) -------------------------------


@pytest.mark.parametrize("door", ["say", "send"])
def test_a_fresh_captain_the_fleet_still_reads_working_is_typed_into_at_its_idle_box(
    captain: tuple[Captain, Clock], door: str
) -> None:
    """T2b (13399, runner2's red-before at 13398): a bare-started real captain reads WORKING
    until its first Stop, and say waited for waiting, so the owner's first say never landed
    ('stayed working for 120s'). The rider's rule (13313): working with the box drawn and
    idle is ready. runner2's real idle capture, verbatim."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.state = "working"  # no Stop hook yet
    fake.screen = list(REAL_IDLE_AFTER_STOP)
    if door == "say":
        assert brain.say("what is up", timeout=60).text == "Nothing needs you right now."
    else:
        brain.send("what is up", timeout=60)
    assert fake.typed == [("paste", "what is up"), ("keys", "Enter")]


@pytest.mark.parametrize("door", ["say", "send"])
def test_a_captain_mid_turn_is_waited_out_never_typed_over(
    captain: tuple[Captain, Clock], door: str
) -> None:
    """The box is drawn DURING a turn too: working with a live spinner above it and 'esc to
    interrupt' in its footer is not ready. runner2's real mid-turn capture, verbatim."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.state = "working"
    fake.screen = list(REAL_WORKING)
    with pytest.raises(brain.NoReply, match="stayed working for 5s") as caught:
        if door == "say":
            brain.say("what is up", timeout=5)
        else:
            brain.send("what is up", timeout=5)
    assert caught.value.timed_out is True and fake.typed == []


@pytest.mark.parametrize("door", ["say", "send"])
def test_both_doors_type_into_a_real_captain_whose_top_rule_carries_its_name(
    captain: tuple[Captain, Clock], door: str
) -> None:
    """T2b (13383): on the RC every say and send to a real captain ended 'never drew its
    prompt' — the box's top rule carries the agent's name, and the reader took bare rules
    only. runner2's real capture, verbatim, as the captain's pane."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = list(REAL_IDLE_AFTER_STOP)
    if door == "say":
        assert brain.say("what is up", timeout=60).text == "Nothing needs you right now."
    else:
        brain.send("what is up", timeout=60)
    assert fake.typed == [("paste", "what is up"), ("keys", "Enter")]


def test_send_types_into_a_waiting_captain_and_returns_without_waiting_for_a_reply(
    captain: tuple[Captain, Clock],
) -> None:
    """T4's What's up types through here: at once, and back before any answer comes."""
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.turn_ends_after = None  # the captain never answers: send must not care
    asked_at = clock.now
    typed_at = brain.send("what is up")
    assert fake.typed == [("paste", "what is up"), ("keys", "Enter")]
    assert typed_at == asked_at and clock.slept == 0.0, "no settle, and no reply wait"
    assert fake.started == [] and fake.told == []


def test_send_never_types_into_the_trust_dialog_and_says_how_to_answer_it(
    captain: tuple[Captain, Clock],
) -> None:
    """The first-run trap through the second door (13325): the Enter picks "No, exit". The
    real capture of Claude Code's trust dialog, as a fresh captain parks at it."""
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.screen = list(REAL_TRUST)
    with pytest.raises(brain.NoReply, match="trust its folder") as caught:
        brain.send("what is up")
    assert "run `aisquare captain` and choose Yes, I trust this folder (once)" in str(caught.value)
    assert caught.value.timed_out is False and fake.typed == [] and clock.slept == 0.0


def test_send_to_a_captain_that_is_not_running_is_said_never_started(
    captain: tuple[Captain, Clock],
) -> None:
    fake, _ = captain
    with pytest.raises(brain.NoReply, match="the captain is not running") as caught:
        brain.send("what is up")
    assert caught.value.timed_out is False
    assert fake.started == [] and fake.typed == []


def test_send_waits_out_a_busy_captain_then_types_never_a_board_note(
    captain: tuple[Captain, Clock],
) -> None:
    fake, clock = captain
    fake.present()  # type: ignore[attr-defined]
    fake.busy_for = 5.0
    fake.busy_screen = list(REAL_WORKING)  # its turn on screen, as a real one shows it
    brain.send("what is up")
    assert fake.typed == [("paste", "what is up"), ("keys", "Enter")]
    assert clock.slept >= 5.0 and fake.told == []


def test_send_says_an_unreachable_captain_at_once(
    captain: tuple[Captain, Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, clock = captain

    def unreachable() -> None:
        raise brain.Unreachable("run `aisquare fleet reap -P prj_home --server-down`")

    monkeypatch.setattr(brain, "find", unreachable)
    with pytest.raises(brain.NoReply, match="--server-down") as caught:
        brain.send("what is up")
    assert caught.value.timed_out is False and fake.typed == [] and clock.slept == 0.0


def test_send_never_types_while_a_say_waits_for_its_reply(captain: tuple[Captain, Clock]) -> None:
    """One delivery at a time: a line typed mid-say would be read as the say's answer."""
    fake, _ = captain
    fake.present()  # type: ignore[attr-defined]
    held = brain._one_at_a_time(brain._now() + timedelta(seconds=5), 5.0)
    with held, pytest.raises(brain.NoReply, match="another message to the captain") as caught:
        brain.send("what is up", timeout=5)
    assert caught.value.timed_out is True and fake.typed == []


# --- the CLI ----------------------------------------------------------------------------------


@dataclass
class Said:
    texts: list[str] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> Said:
    record = Said()

    def fake_say(text: str, *, timeout: float = 180.0) -> brain.Reply:
        record.texts.append(text)
        record.timeouts.append(timeout)
        if text == "silence":
            raise brain.NoReply(
                "the captain did not answer within 5s — its answer will be in its pane"
            )
        if text == "dead":
            raise brain.NoReply("the captain exited before it answered", timed_out=False)
        if text == "refused":
            raise fleet.FleetError("the home already has a captain (agt_x) — one per home")
        if text == "tools only":
            return brain.Reply(text=None, ended_at=datetime(2026, 9, 25, 10, 6, tzinfo=UTC))
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


def test_options_before_the_text_still_reach_say(runner: CliRunner, said: Said) -> None:
    result = runner.invoke(app, ["captain", "--timeout", "30", "what", "is", "up"])
    assert result.exit_code == 0, result.output
    assert (said.texts, said.timeouts) == (["what is up"], [30.0])


def test_a_message_that_starts_with_a_dash_goes_after_a_double_dash(
    runner: CliRunner, said: Said
) -> None:
    result = runner.invoke(app, ["captain", "--", "-5 degrees outside"])
    assert result.exit_code == 0, result.output
    assert said.texts == ["-5 degrees outside"]


def test_captain_help_is_the_groups_own(runner: CliRunner, said: Said) -> None:
    result = runner.invoke(app, ["captain", "--help"])
    assert result.exit_code == 0
    assert said.texts == []
    assert "chat" in result.output and "serve" in result.output


def test_captain_text_json_says_an_unreachable_captain_is_not_a_timeout(
    runner: CliRunner, said: Said
) -> None:
    result = runner.invoke(app, ["--json", "captain", "dead"])
    assert result.exit_code == 1
    body = json.loads(result.stdout)
    assert (body["reply"], body["timed_out"]) == (None, False)
    assert body["said"] == "the captain exited before it answered"


def test_captain_text_json_keeps_the_reason_the_fleet_refused(
    runner: CliRunner, said: Said
) -> None:
    result = runner.invoke(app, ["--json", "captain", "refused"])
    assert result.exit_code == 1
    assert "already has a captain" in result.stdout


def test_bare_captain_under_json_prints_one_object_and_never_attaches(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.cli import fleet as fleet_cli

    row = FleetAgent(
        id="agt_c", project_id="prj_home", label="captain", role="captain",
        pane_id="%1", cwd=Path("/tmp"), created_at=datetime.now(tz=UTC),
    )  # fmt: skip
    monkeypatch.setattr(brain, "find", lambda: row)
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)
    monkeypatch.setattr(fleet_cli, "_exec_attach", lambda argv: pytest.fail("exec'd under --json"))
    monkeypatch.setattr(fleet, "attach_argv", lambda project: ["tmux", "attach", "-t", "asq-h"])
    result = runner.invoke(app, ["--json", "captain"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["agent"]["id"] == "agt_c"
    assert body["started"] is False
    assert body["argv"] == ["tmux", "attach", "-t", "asq-h"]


def test_bare_captain_says_a_tmux_it_cannot_run(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.cli import fleet as fleet_cli

    row = FleetAgent(
        id="agt_c", project_id="prj_home", label="captain", role="captain",
        pane_id="%1", cwd=Path("/tmp"), created_at=datetime.now(tz=UTC),
    )  # fmt: skip

    def gone(argv: list[str]) -> None:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(brain, "find", lambda: row)
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)
    monkeypatch.setattr(fleet_cli, "_exec_attach", gone)
    monkeypatch.setattr(fleet, "attach_argv", lambda project: ["tmux", "attach", "-t", "asq-h"])
    result = runner.invoke(app, ["captain"])
    assert result.exit_code == 1
    assert "could not run tmux" in result.output


def test_a_reply_without_text_is_null_under_json_and_said_why(
    runner: CliRunner, said: Said
) -> None:
    result = runner.invoke(app, ["--json", "captain", "tools only"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert (body["reply"], body["timed_out"]) == (None, False)
    assert body["ended_at"] == "2026-09-25T10:06:00+00:00"
    assert body["said"] == "the captain's turn ended without text — its pane shows what it did"


def test_a_reply_without_text_prints_nothing_as_the_captains_words(
    runner: CliRunner, said: Said
) -> None:
    result = runner.invoke(app, ["captain", "tools only"])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "ended without text" in result.stderr
