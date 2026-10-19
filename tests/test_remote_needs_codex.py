"""Remote's needs-you cards, quick answers and Transcript tab for a Codex agent (SCOPE #113
0.8.1, items 1, 1a, 1b, 2 and 3).

Each test goes through the store, the fleet's listing and the live sources, as the phone's
scan does, over a rollout Codex 0.162.1 wrote: ``tests/fixtures/agent_transcripts/
codex-0.162.1.jsonl``, measured with a loopback provider and the TUI under tmux (an approval
answered with Esc and then with ``1``, a turn that ended on a question, ``request_user_input``
in Plan mode answered with ``2``, a plan proposed), its paths sanitized. Each moment of that
session is the rollout as it stood then: its first lines, their times moved to end a
little before the scan.

Before the transcript slot, Remote read every transcript as Claude Code's JSONL: a rollout
said nothing, so no card came from it, its page was empty, and a rollout past the tail's
budget read as a walk cut short, a tool pending for good.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.config import AccountsSettings
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamEvent, TeamSession
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_needs, transcript
from aisquare.services.remote_needs import (
    needs_agent_now,
    needs_dialog_open,
    needs_from_agent,
    needs_tool_pending,
    scan_needs_you,
)
from aisquare.services.remote_server import Runtime, build_app
from aisquare.services.remote_server import _live_transcript as live_transcript
from aisquare.services.transcript import MAX_LINE, TAIL_BUDGET
from tests.remote_kit_helpers import base, make_client, make_runtime, unlock
from tests.test_remote_needs import FakeTmux, _rooted

ROLLOUT = Path(__file__).parent / "fixtures" / "agent_transcripts" / "codex-0.162.1.jsonl"

#: Each moment of the measured session: how many of the rollout's lines it had written.
MOMENTS = {
    "approval": 10,  # exec_command asking for escalation, its approval view up
    "interrupted": 14,  # that approval answered with Esc: the turn aborted
    "at_work": 21,  # the next one approved with 1: the command running
    "asked": 29,  # that turn over, its last words "… Shall I go on?"
    "question": 53,  # request_user_input in Plan mode, its question view up
    "answered": 61,  # answered with 2, the turn over
    "planned": 74,  # a plan proposed, the session as it ended
}

CODEX = get_adapter("codex")


def _records(moment: str) -> list[dict[str, Any]]:
    lines = ROLLOUT.read_text(encoding="utf-8").splitlines()[: MOMENTS[moment]]
    return [json.loads(line) for line in lines]


def _stamp(record: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))


def _moved(records: list[dict[str, Any]], newest: datetime) -> list[dict[str, Any]]:
    """``records`` with every time moved by the one distance that makes the newest ``newest``."""
    by = newest - max(_stamp(record) for record in records)
    return [
        {**record, "timestamp": (_stamp(record) + by).isoformat().replace("+00:00", "Z")}
        for record in records
    ]


def _write(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _moment(tmp_path: Path, moment: str, newest: datetime) -> tuple[Path, list[dict[str, Any]]]:
    records = _moved(_records(moment), newest)
    return _write(tmp_path / f"rollout-{moment}.jsonl", records), records


def _kind(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    return str(payload.get("type")) if isinstance(payload, dict) else str(record.get("type"))


def _codex_agent(
    project: ProjectInfo,
    *,
    state: str,
    seen: datetime,
    born: datetime,
    rollout: Path,
    label: str = "coder-1",
    events: tuple[tuple[str, str, datetime], ...] = (),
    resets: datetime | None = None,
    slot: int | None = None,
) -> FleetAgent:
    """``label``'s Codex row and its board session in the store, and the session's events."""
    session = TeamSession(
        id=f"ses_{label}", project_id=project.id, role="coder", label=label, started_at=born,
        last_seen_at=seen, state=state, limit_resets_at=resets, transcript_path=str(rollout),
    )  # fmt: skip
    row = FleetAgent(
        id=f"agt_{label}", project_id=project.id, label=label, role="coder", agent="codex",
        binary="codex", pane_id="%1", session_id=session.id, cwd=project.root, created_at=born,
        account_slot=slot,
    )  # fmt: skip
    with store_session() as store:
        store.upsert_session(session)
        store.upsert_fleet_agent(row)
        for n, (kind, text, at) in enumerate(events):
            store.add_team_event(
                TeamEvent(
                    id=f"evt_{label}_{n}", project_id=project.id, session_id=session.id,
                    kind=kind, text=text, created_at=at,
                )
            )  # fmt: skip
    return row


@pytest.fixture
def project(tmp_path: Path) -> ProjectInfo:
    with store_session() as store:
        return store.onboard_project(_rooted(tmp_path / "alpha"))


@pytest.fixture
def tmux(monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    """Codex's pane, quiet (an approval view redraws, but tmux's activity is the board's
    to weigh): the pane's foreground is ``codex``."""
    pane = FakeTmux(command="codex", quiet_for=120)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: pane)
    return pane


@pytest.fixture
def runtime() -> Runtime:
    return make_runtime()


def _cards(now: datetime) -> list[Any]:
    return scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())


def _answers(card: Any) -> list[tuple[str, tuple[str, ...]]]:
    return [(answer.label, answer.keys) for answer in card.answers]


# --- the cards: each rule's Codex shape ---------------------------------------------------------


def test_a_codex_approval_is_a_permission_card_with_its_dialogs_keys(
    tmp_path: Path, project: ProjectInfo, tmux: FakeTmux
) -> None:
    """Rule 5: the call its approval view is for, waiting without a result, and the board's
    attention (its PermissionRequest hook). It was rule 8's "shows a dialog that needs you",
    no call named and no quick answer: the rollout read as nothing."""
    now = datetime.now(UTC)
    rollout, records = _moment(tmp_path, "approval", now - timedelta(minutes=2))
    asked_at = _stamp(records[-1]) + timedelta(milliseconds=40)
    _codex_agent(
        project, state="attention", seen=asked_at, born=now - timedelta(hours=1), rollout=rollout,
        events=(("attention", "Codex needs your approval", asked_at),),
    )  # fmt: skip
    (card,) = _cards(now)
    assert (card.kind, card.reason) == (
        "permission",
        "coder-1 waits for a permission answer to use exec_command",
    )
    assert card.detail["tool"] == "exec_command"
    shown = card.detail["input"]
    assert shown["cmd"] == "touch /work/outside-probe"
    assert shown["sandbox_permissions"] == "require_escalated"
    assert card.excerpt == "exec_command(touch /work/outside-probe)"
    # Measured: 1 approves ("Yes, proceed"), Esc is "No" and stops the turn.
    assert _answers(card) == [("1", ("1",)), ("2", ("2",)), ("No", ("Escape",))]
    assert card.id == remote_needs.needs_item_id(project.id, "permission", "call_1")


def test_a_codex_question_is_a_question_card_its_options_on_their_digits(
    tmp_path: Path, project: ProjectInfo, tmux: FakeTmux
) -> None:
    """Rule 3: a ``request_user_input`` waiting for its answer is the question, its dialog the
    call itself: every question in the card, each option on the digit Codex's view picks it
    with (measured: ``2`` picked Blue and sent it), Esc to stop."""
    now = datetime.now(UTC)
    rollout, records = _moment(tmp_path, "question", now - timedelta(minutes=2))
    asked_at = _stamp(records[-1])
    _codex_agent(
        project, state="attention", seen=asked_at, born=now - timedelta(hours=1), rollout=rollout,
        events=(("attention", "Codex needs user input", asked_at),),
    )  # fmt: skip
    (card,) = _cards(now)
    assert (card.kind, card.reason) == ("question", "coder-1 asks you a question")
    assert card.excerpt == "Which colour should the button be? — Red (Recommended) · Blue"
    (question,) = card.detail["questions"]
    assert question["header"] == "Colour" and question["multiSelect"] is False
    assert [option["label"] for option in question["options"]] == ["Red (Recommended)", "Blue"]
    assert _answers(card) == [
        ("Red (Recommended)", ("1",)),
        ("Blue", ("2",)),
        ("Cancel", ("Escape",)),
    ]


def test_a_codex_turn_stopped_at_its_approval_is_an_interrupted_card(
    tmp_path: Path, project: ProjectInfo, tmux: FakeTmux
) -> None:
    """Rule 6: the rollout ends on the turn's ``turn_aborted``. Codex's Interrupt hook marks
    the session ``waiting`` first (measured on 0.162.1: 40 to 46 ms before the record, in
    four tries), so the interruption is later than the session's last hook, as Claude Code's
    is, where an Esc fires no hook at all."""
    now = datetime.now(UTC)
    rollout, records = _moment(tmp_path, "interrupted", now - timedelta(minutes=2))
    aborted = records[-1]
    assert _kind(aborted) == "turn_aborted"
    hooked = _stamp(aborted) - timedelta(milliseconds=45)
    _codex_agent(
        project, state="waiting", seen=hooked, born=now - timedelta(hours=1), rollout=rollout,
        events=(("attention", "Codex needs your approval", hooked - timedelta(seconds=19)),),
    )  # fmt: skip
    (card,) = _cards(now)
    assert (card.kind, card.reason) == ("interrupted", "coder-1 was interrupted and waits for you")
    assert card.since == _stamp(aborted)
    assert card.push_after == card.since + timedelta(minutes=10)


def test_a_codex_turn_that_ended_on_a_question_is_an_asked_card(
    tmp_path: Path, project: ProjectInfo, tmux: FakeTmux
) -> None:
    """Rule 9: the turn's ``task_complete`` after the agent's last words, which end on a
    question, and the Stop hook's ``waiting``."""
    now = datetime.now(UTC)
    rollout, records = _moment(tmp_path, "asked", now - timedelta(minutes=2))
    stopped = _stamp(records[-1]) + timedelta(milliseconds=200)
    _codex_agent(
        project, state="waiting", seen=stopped, born=now - timedelta(hours=1), rollout=rollout
    )
    (card,) = _cards(now)
    assert (card.kind, card.reason) == ("asked", "coder-1 ended its turn with a question")
    assert card.excerpt.endswith("Shall I go on?")


def test_a_codex_command_at_work_is_a_pending_tool_to_the_guards_and_no_card(
    tmp_path: Path, project: ProjectInfo, tmux: FakeTmux
) -> None:
    """A command approved and running has no result yet: no card (the board says no prompt
    is up), but a stop, a tell or a guarded key reads its tool pending, as for Claude Code.
    Read as Claude Code's JSONL, the rollout held no tool at all."""
    now = datetime.now(UTC)
    rollout, records = _moment(tmp_path, "at_work", now - timedelta(seconds=5))
    _codex_agent(
        project, state="working", seen=_stamp(records[-1]), born=now - timedelta(hours=1),
        rollout=rollout,
    )  # fmt: skip
    assert _cards(now) == []
    snap = needs_agent_now(project, "coder-1", now=now)
    assert snap.tail is not None
    assert [tool.tool_use_id for tool in snap.tail.pending] == ["call_3"]
    assert needs_tool_pending(snap)


@pytest.mark.parametrize(("moment", "kinds"), [("answered", ["asked"]), ("planned", [])])
def test_a_codex_turn_that_ended_holds_no_call(
    tmp_path: Path, project: ProjectInfo, tmux: FakeTmux, moment: str, kinds: list[str]
) -> None:
    """A question answered and the turn over (its last words asking again), a plan proposed:
    no call waits, to the cards or to the guards."""
    now = datetime.now(UTC)
    rollout, records = _moment(tmp_path, moment, now - timedelta(minutes=2))
    _codex_agent(
        project, state="waiting", seen=_stamp(records[-1]), born=now - timedelta(hours=1),
        rollout=rollout,
    )  # fmt: skip
    assert [card.kind for card in _cards(now)] == kinds
    snap = needs_agent_now(project, "coder-1", now=now)
    assert not needs_tool_pending(snap) and not needs_dialog_open(snap)


# --- defect 1(b): a rollout past the tail's budget ---------------------------------------------


def _long_finished_rollout(tmp_path: Path, newest: datetime) -> Path:
    """A rollout of 600 KiB and more whose last turn ended: an earlier turn's command
    outputs (each under ``MAX_LINE``) before the measured session's turn that ended asking."""
    template = _records("asked")
    earlier: list[dict[str, Any]] = []
    for n in range(5):
        call = {"type": "function_call", "name": "exec_command", "call_id": f"call_old_{n}"}
        call["arguments"] = json.dumps({"cmd": f"cat part-{n}"})
        out = {"type": "function_call_output", "call_id": f"call_old_{n}"}
        out["output"] = "y" * (MAX_LINE - 2_000)
        earlier += [
            {"timestamp": template[1]["timestamp"], "type": "response_item", "payload": call},
            {"timestamp": template[1]["timestamp"], "type": "response_item", "payload": out},
        ]
    records = _moved([*template[:1], *earlier, *template[1:]], newest)
    path = _write(tmp_path / "rollout-long.jsonl", records)
    assert path.stat().st_size > 600 * 1024 > TAIL_BUDGET, "the premise"
    return path


def test_a_long_codex_rollout_whose_last_turn_ended_holds_no_pending_tool(
    tmp_path: Path, project: ProjectInfo, tmux: FakeTmux
) -> None:
    """Defect 1(b) of the SCOPE note: a rollout over ``TAIL_BUDGET`` never met a record the
    Claude Code walk reads, so the walk ran out of its budget with no assistant message seen,
    ``cut_at`` was the file's mtime, and ``needs_tool_pending``/``needs_dialog_open`` held
    for any long Codex session: prompt and interrupt tells, restart and switch refused from
    the phone, "has a tool pending". The turn's ``task_complete`` says it ended."""
    now = datetime.now(UTC)
    rollout = _long_finished_rollout(tmp_path, now - timedelta(minutes=2))
    _codex_agent(
        project, state="waiting", seen=now - timedelta(minutes=2), born=now - timedelta(hours=1),
        rollout=rollout,
    )  # fmt: skip
    snap = needs_agent_now(project, "coder-1", now=now)
    assert snap.tail is not None and snap.tail.cut_at is None and snap.tail.pending == ()
    assert not needs_tool_pending(snap)
    assert not needs_dialog_open(snap)
    (card,) = _cards(now)
    assert card.kind == "asked", "its last words are read past the earlier turn's outputs"


def test_a_codex_rollout_cut_inside_its_newest_response_says_cut_and_names_no_tool(
    tmp_path: Path, project: ProjectInfo, tmux: FakeTmux
) -> None:
    """A walk that ran out inside the newest response of a turn still running may have missed
    a call of it still waiting: ``cut_at``, which the guards read as a tool pending, and no
    tool named, so no permission card is made of it (there is none to show)."""
    now = datetime.now(UTC)
    template = _records("approval")
    call = {"type": "function_call", "name": "exec_command", "call_id": "call_big"}
    call["arguments"] = json.dumps({"cmd": "cat huge.log"})
    big = {"type": "function_call_output", "call_id": "call_big", "output": "z" * TAIL_BUDGET}
    at = template[8]["timestamp"]
    records = [
        *template[:8],
        {"timestamp": at, "type": "response_item", "payload": call},
        {"timestamp": at, "type": "response_item", "payload": big},
    ]
    rollout = _write(tmp_path / "rollout-cut.jsonl", _moved(records, now - timedelta(minutes=1)))
    tail = transcript.read_transcript_tail(rollout, agent="codex")
    assert tail is not None and tail.pending == ()
    written = datetime.fromtimestamp(rollout.stat().st_mtime, tz=UTC)
    assert tail.cut_at == written, "cut: what it did not read may hold a call still waiting"
    _codex_agent(
        project, state="working", seen=now - timedelta(minutes=1), born=now - timedelta(hours=1),
        rollout=rollout,
    )  # fmt: skip
    snap = needs_agent_now(project, "coder-1", now=now)
    assert needs_tool_pending(snap)
    assert _cards(now) == []


# --- defect 1(a): the hand-over's question names the agent -------------------------------------


def test_a_limited_codex_agent_counts_no_claude_account_as_somewhere_to_go(
    isolated_home: Path, project: ProjectInfo
) -> None:
    """Defect 1(a) of the SCOPE note: the scan asked ``somewhere_to_hand_over(leaving)`` with
    no agent, where the limit's hook asks it with the row's (``hooks.py``). With Claude Code
    accounts signed in and no other Codex one, the hook hands nothing over, while the scan
    counted the Claude accounts and held the limit's push 90 s for a hand-over that never
    came. The live sources ask with the row's agent now, as the hook does."""
    from aisquare.services import claude_accounts as accounts_service

    codex_slot = accounts_core.create_account("codex")
    (codex_slot.config_dir / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-test"}), encoding="utf-8"
    )
    for email in ("one@example.com", "two@example.com"):
        claude = accounts_core.create_account()
        (claude.config_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": email}}), encoding="utf-8"
        )
        (claude.config_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "test-token"}}), encoding="utf-8"
        )
    assert accounts_service.somewhere_to_hand_over(codex_slot.slot), "Claude accounts: room"
    now = datetime.now(UTC)
    resets = now + timedelta(hours=3)
    row = FleetAgent(
        id="agt_coder-1", project_id=project.id, label="coder-1", role="coder", agent="codex",
        binary="codex", pane_id="%1", session_id="ses_coder-1", cwd=project.root,
        created_at=now - timedelta(hours=1), account_slot=codex_slot.slot,
    )  # fmt: skip
    session = TeamSession(
        id="ses_coder-1", project_id=project.id, role="coder", started_at=row.created_at,
        last_seen_at=now - timedelta(minutes=1), state="limited", limit_resets_at=resets,
        account=str(codex_slot.config_dir),
    )  # fmt: skip
    status = FleetAgentStatus(agent=row, state="limited", session=session)
    sources = remote_needs.live_needs_sources()
    (card,) = needs_from_agent(
        status,
        None,
        project=project,
        events=[],
        now=now,
        accounts=AccountsSettings(on_limit="switch"),
        hands_over=sources.hands_over,
    )
    assert card.kind == "limited"
    assert card.push_after == card.since, "no Codex account to go to: pushed at once"


# --- the Transcript tab ------------------------------------------------------------------------


def _plain(lines: list[str]) -> list[str]:
    import re

    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def _page(label: str, project: ProjectInfo, before: str | None = None) -> dict[str, Any]:
    return live_transcript(label, project.id, 200, before, 60)


def test_a_codex_rollout_is_a_page_of_its_turns_under_its_own_speaker(
    tmp_path: Path, project: ProjectInfo
) -> None:
    """The Transcript tab read every transcript as Claude Code's JSONL: a Codex page was
    empty, and its speaker would have been ``* claude``. The human's words, the agent's, each
    call as one line, a question in full, an interruption and a proposed plan."""
    now = datetime.now(UTC)
    rollout, _ = _moment(tmp_path, "planned", now - timedelta(minutes=2))
    _codex_agent(
        project, state="waiting", seen=now - timedelta(minutes=2), born=now - timedelta(hours=1),
        rollout=rollout,
    )  # fmt: skip
    page = _page("coder-1", project)
    lines = _plain(page["lines"])
    assert page["more"] is False and page["cursor"] is None
    assert "* claude" not in lines
    assert lines[:5] == ["> you", "  ESC touch /work/outside-probe", "", "* codex", lines[4]]
    assert lines[4].startswith("  ⎿ exec_command(touch /work/outside-probe")
    assert "  ⎿ interrupted" in lines
    assert "  ? Colour: Which colour should the button be?" in lines
    assert "    1. Red (Recommended) — Warm and loud" in lines
    assert "  ⎿ plan:" in lines and "    # Paint the button" in lines
    assert not any("environment_context" in line or "turn_aborted" in line for line in lines)
    assert page["stamps"]["0"].endswith("+00:00")


def test_load_older_on_a_codex_rollout_reads_one_budget_a_page_and_reaches_its_start(
    tmp_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each Load older reads at most ``SCAN_BUDGET`` bytes back, past outputs that render
    nothing (three of a megabyte here, each one line), and the pages together are the whole
    conversation, oldest turn included, each request's cursor further back."""
    now = datetime.now(UTC)
    template = _records("planned")
    at = template[20]["timestamp"]
    padding = [
        {
            "timestamp": at,
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": f"pad_{n}",
                "output": "p" * 1_000_000,
            },
        }
        for n in range(3)
    ]
    records = _moved([*template[:20], *padding, *template[20:]], now - timedelta(minutes=2))
    rollout = _write(tmp_path / "rollout-padded.jsonl", records)
    _codex_agent(
        project, state="waiting", seen=now - timedelta(minutes=2), born=now - timedelta(hours=1),
        rollout=rollout,
    )  # fmt: skip
    read: list[int] = []
    real = transcript.lines_backwards

    def counted(file: Path, end: int, *, budget: int) -> Any:
        read.append(budget)
        return real(file, end, budget=budget)

    monkeypatch.setattr(transcript, "lines_backwards", counted, raising=False)
    pages: list[list[str]] = []
    cursor: str | None = None
    for _ in range(10):
        page = _page("coder-1", project, cursor)
        pages.insert(0, _plain(page["lines"]))
        if not page["more"]:
            break
        assert page["cursor"] is not None and (cursor is None or page["cursor"] != cursor)
        cursor = page["cursor"]
    else:
        raise AssertionError("Load older never reached the start")
    assert all(budget == transcript.SCAN_BUDGET for budget in read)
    assert len(read) <= math.ceil(rollout.stat().st_size / transcript.SCAN_BUDGET) + 1
    whole = [line for page in pages for line in page]
    assert whole[:2] == ["> you", "  ESC touch /work/outside-probe"], "its oldest turn"
    assert "  ⎿ plan:" in whole, "its newest"


# --- a quick answer waits for the dialog it answers ------------------------------------------

#: Codex 0.162.1's screens as ``capture-pane -e`` gives them (``--no-alt-screen``), measured:
#: a command at work, its status line and composer styled, and the question view
#: ``request_user_input`` draws once its hooks have run.
_DIM, _BOLD, _OFF = "\x1b[2m", "\x1b[1m", "\x1b[0m"
WORKING_SCREEN = [
    "  >_ OpenAI Codex (v0.162.1)",
    "     ~/alpha",
    f"{_BOLD}\u203a{_OFF} ASK",
    f"\u2022 {_DIM}Working{_OFF} {_DIM}(2s \u2022 {_OFF}{_BOLD}esc{_OFF}{_DIM} to interrupt)"
    f" \u00b7 1 background terminal running \u00b7 /ps to view \u00b7 /stop to close{_OFF}",
    f"{_BOLD}\u203a{_OFF} {_DIM}Ask Codex to do anything{_OFF}",
    "  local-fixture medium \u00b7 ~/alpha \u00b7 \u2190 for agents              Plan mode",
]
#: The same, below the agent's words with a numbered list in them: a row ``1. …`` that a
#: dialog's first option would look like, so only Codex's own reading of its screen (its
#: working line) says no dialog is drawn.
LISTED_SCREEN = [
    *WORKING_SCREEN[:3],
    "\u2022 Two steps:",
    "  1. Make it blue",
    "  2. Ship it",
    *WORKING_SCREEN[3:],
]
QUESTION_SCREEN = [
    "  >_ OpenAI Codex (v0.162.1)",
    "     ~/alpha",
    f"{_BOLD}\u203a{_OFF} ASK",
    "  Question 1/1 (1 unanswered)",
    f"  {_BOLD}Which colour should the button be?{_OFF}",
    f"  {_BOLD}\u203a 1. Red (Recommended){_OFF}  {_DIM}Warm and loud{_OFF}",
    f"    2. Blue               {_DIM}Calm{_OFF}",
    f"    3. None of the above  {_DIM}Optionally, add details in notes (tab){_OFF}",
    f"  {_DIM}tab to add notes | enter to submit answer | esc to interrupt{_OFF}",
]


@pytest.mark.parametrize("early_screen", [WORKING_SCREEN, LISTED_SCREEN], ids=["working", "listed"])
def test_a_codex_questions_digit_waits_until_its_view_is_drawn(
    tmp_path: Path,
    project: ProjectInfo,
    tmux: FakeTmux,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    early_screen: list[str],
) -> None:
    """Codex records ``request_user_input`` as it streams and draws its question view only
    once the call's PreToolUse hook has run: the card came first, and its ``2`` tapped at
    once was typed into the composer under the view, which waited on, unanswered (measured
    on 0.162.1, the live test of this page). With Codex's screen at work, the tap is refused
    as no prompt, nothing typed, also under a numbered list of the agent's that looks like
    a dialog's first option, and read as tmux captures it, escapes and all; with its view
    drawn, the digit goes in."""
    now = datetime.now(UTC)
    rollout, records = _moment(tmp_path, "question", now - timedelta(seconds=1))
    asked_at = _stamp(records[-1])
    _codex_agent(
        project, state="working", seen=asked_at, born=now - timedelta(hours=1), rollout=rollout
    )
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 0.0)
    runtime.set_allow_write(True)
    client = make_client(build_app(runtime, dist_dir=tmp_path))
    assert unlock(client, runtime).status_code == 200
    api = f"{base(runtime)}/api"
    tmux.screen = list(early_screen)
    (card,) = client.get(f"{api}/needs").json()["items"]
    assert card["kind"] == "question" and card["answers"][1] == {"label": "Blue", "keys": ["2"]}

    early = client.post(f"{api}/needs/answer", json={"id": card["id"], "keys": ["2"]})
    assert early.status_code == 409 and early.json()["error"] == "stale", early.text
    assert "shows no prompt now" in early.json()["message"]
    assert tmux.typed == [], "nothing typed under a view not drawn yet"

    tmux.screen = list(QUESTION_SCREEN)
    (card,) = client.get(f"{api}/needs").json()["items"]
    drawn = client.post(f"{api}/needs/answer", json={"id": card["id"], "keys": ["2"]})
    assert drawn.status_code == 200, drawn.text
    assert tmux.typed == [("keys", "%1", "2")]
