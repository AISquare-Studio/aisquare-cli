"""Remote's agent actions, key pad and explainability card for a Codex agent (SCOPE #113 0.8.1,
items 4, 5 and 7), and the one Ctrl-C or Ctrl-D that exits it at its empty prompt.

The phone's writes go through the real handlers, needs-you's predicates and its snapshot
(``remote_needs._needs_snapshot``, which asks the pane); only the project's scan is faked
down to the one row (:class:`OneRowScan`), and tmux to a pane showing a screen measured on
Codex 0.162.1 under tmux 3.7c. A Claude Code row is the control wherever the change could
reach it: its snapshot reads no screen, and its answers are the ones its own tests pin.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import screen_of
from aisquare.core.config import ExplainabilitySettings, ExplainabilityTarget, load_config
from aisquare.core.config import save_config as save_config_file
from aisquare.core.store import SqliteStore, store_session
from aisquare.core.tmux import TmuxError
from aisquare.models import (
    CheckStatus,
    DoctorCheck,
    FleetAgentState,
    FleetAgentStatus,
    ProjectInfo,
    TeamSession,
)
from aisquare.services import explainability_ops as ops
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_needs, remote_server
from aisquare.services.explainability import KEY_ENV_VAR
from aisquare.services.remote_needs import AgentNow
from aisquare.services.transcript import TranscriptTail
from tests.test_remote_actions import (  # noqa: F401 - fixtures, by name
    LABEL,
    PINNED,
    FakePane,
    FleetCalls,
    Phone,
    _agent,
    clock,
    fleet,
    log,
    phone,
    project,
    receipts,
    runtime,
)
from tests.test_remote_explainability import fleet_home  # noqa: F401 - fixture, by name

CODEX = get_adapter("codex")
CHEVRON = "\u203a"
FOOTER = "  local-fixture default · /work/repo"
#: Codex's empty prompt in its alternate screen, after a turn (0.162.1, measured).
IDLE = [
    f"{CHEVRON} say hi",
    "• ok",
    "  Worked for <1s • 2:52 PM",
    f"{CHEVRON} Ask Codex to do anything",
    FOOTER,
    "  ? for shortcuts                                       ⚠ 2 warnings · f2 to view",
]
#: The same prompt after one Esc: until the next key, the footer's hint is this one.
ESC_AGAIN = [*IDLE[:-1], "  esc again to edit previous message"]
#: A second Esc: its transcript, where Enter rewinds the conversation.
BROWSING = [
    *IDLE[:-1],
    "  Browsing transcript · ↑↓/jk scroll · ←→/hl prompts · ^t details · ↵ rewind · esc back",
]
#: The dialog Codex opens after a turn's Stop near its limit, its hooks saying waiting.
SWITCH_DIALOG = [
    "• ok",
    "  Worked for <1s • 11:29 PM",
    "  Approaching rate limits",
    "  Switch to gpt-6-luna for lower credit usage?",
    f"{CHEVRON} 1. Switch to gpt-6-luna                   Fast and affordable model",
    "  2. Keep current model",
    "  3. Keep current model (never show again)  Hide future rate limit reminders",
    "  enter select · esc back",
]
#: The human's half-typed line in the composer: a typed line would join it.
DRAFT = [*IDLE[:3], f"{CHEVRON} half a sentence of the human's", FOOTER]
#: A turn running: the status line above the composer, the empty prompt kept under it.
WORKING = [f"{CHEVRON} SLOW one", "• Working (2s • esc to interrupt)", *IDLE[3:]]
#: What a Codex rollout reads as through the Claude transcript reader: no record it knows.
ROLLOUT_UNREAD = TranscriptTail(
    pending=(), newest="none", newest_at=None, last_text=None, last_text_at=None, marker_key=None
)


class CodexPane(FakePane):
    """The agent's tmux, its pane showing ``screen`` (``screen_text``), printing nothing for a
    minute unless ``busy`` (``pane_facts``), on a server of ``version``. An Escape shows
    ``after_escape``, as Codex redraws on one, and with ``rests_on_escape`` stops the pane
    printing (a turn it stopped)."""

    def __init__(self, trail: list[str]) -> None:
        super().__init__(trail)
        self.screen: list[str] = IDLE
        self.after_escape: list[str] | None = None
        self.rests_on_escape = False
        self.busy = False
        self.unreadable = False
        self.version: tuple[int, int] | None = (3, 7)
        self.command = "codex"

    def screen_text(self, pane_id: str) -> list[str]:
        if self.unreadable:
            raise TmuxError("capture-pane: server exited unexpectedly")
        return list(self.screen)

    def pane_facts(self, pane_id: str) -> SimpleNamespace:
        now = datetime.now(UTC)
        return SimpleNamespace(
            dead=False,
            current_command=self.command,
            server_started=self.started_at(),
            last_output=now - (timedelta(seconds=1) if self.busy else timedelta(minutes=1)),
        )

    def send_keys(self, pane_id: str, *keys: str) -> None:
        super().send_keys(pane_id, *keys)
        if "Escape" in keys and self.after_escape is not None:
            self.screen = self.after_escape
            self.busy = self.busy and not self.rests_on_escape

    def server_version(self) -> tuple[int, int] | None:
        return self.version


@pytest.fixture
def pane(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> CodexPane:  # noqa: F811
    fake = CodexPane(log)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: fake)
    # The store keeps no launch records here: placement has tests of its own.
    monkeypatch.setattr(fleet_service, "placed_row", lambda row: row)
    return fake


class OneRowScan:
    """needs-you's reads of the project, down to the label's newest row: its status as the
    listing derives it (``state``, ``session``) and its ``tail``, no items. The snapshot is
    the module's own (``_needs_snapshot``), which asks the pane what it asks."""

    def __init__(self) -> None:
        self.state: FleetAgentState = "waiting"
        self.session: TeamSession | None = None
        self.tail: TranscriptTail | None = None
        self.snaps: list[AgentNow] = []

    def __call__(self, target: ProjectInfo, label: str, *, now: datetime | None = None) -> AgentNow:
        with store_session() as store:
            row = store.fleet_agent_by_label(target.id, label, live_only=False)
        assert row is not None
        status = FleetAgentStatus(agent=row, state=self.state, session=self.session)
        snap = remote_needs._needs_snapshot(target, status, self.tail, (), now or datetime.now(UTC))
        self.snaps.append(snap)
        return snap


@pytest.fixture
def scan(monkeypatch: pytest.MonkeyPatch, clock: Any) -> OneRowScan:  # noqa: F811
    fake = OneRowScan()
    monkeypatch.setattr(remote_needs, "needs_agent_now", fake)
    monkeypatch.setattr(remote_needs, "needs_single_agent_now", fake)
    return fake


def _row(project: ProjectInfo, agent: str | None = "codex", session: str | None = None) -> None:  # noqa: F811
    with store_session() as store:
        store.upsert_fleet_agent(
            _agent(project).model_copy(update={"agent": agent, "session_id": session})
        )


def _stopped_turn(project: ProjectInfo) -> TeamSession:  # noqa: F811
    """The board session of a Codex whose turn's ``Stop`` just fired: waiting, fresh."""
    now = datetime.now(UTC)
    return TeamSession(
        id="ses_codex",
        project_id=project.id,
        role="coder",
        started_at=now - timedelta(minutes=5),
        last_seen_at=now - timedelta(seconds=20),
        state="waiting",
    )


def _at_its_dialog(project: ProjectInfo, scan: OneRowScan, pane: CodexPane) -> None:  # noqa: F811
    _row(project, session="ses_codex")
    scan.session, scan.state, scan.tail = _stopped_turn(project), "waiting", ROLLOUT_UNREAD
    pane.screen = SWITCH_DIALOG


# --- tell: what the paste and its Enter would answer ------------------------------------------


@pytest.mark.parametrize("mode", ["prompt", "interrupt"])
def test_a_codex_at_the_dialog_it_opened_after_its_stop_is_told_nothing(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
    mode: str,
) -> None:
    """Codex opens ``Switch to gpt-6-luna for lower credit usage?`` after a turn's ``Stop``,
    which no hook reports: its row reads waiting, its pane is quiet, and its rollout reads as
    nothing said. Prompt mode pasted into the dialog and its Enter took option 1, the model
    switched for the human; Interrupt & tell sent its Escape and then did the same. Its
    screen is not its empty prompt: refused before anything reaches it."""
    _at_its_dialog(project, scan, pane)
    response = phone.post("agent/tell", agent=LABEL, text="carry on", mode=mode)
    assert response.status_code == 409, response.text
    assert response.json() == {
        "error": "dialog_open",
        "message": "coder-1 is showing a prompt; typing now would answer it — answer it or "
        "dismiss it first",
    }
    assert pane.sent == [], "no Escape, no paste, no Enter"
    assert scan.snaps[0].screen == tuple(SWITCH_DIALOG)


def test_a_fresh_codex_at_its_empty_prompt_takes_a_prompt_tell(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Codex fires no hook before its first prompt, so a fresh one has no board session and
    no transcript to go by: refused as an agent nothing reads ("use Interrupt & tell"),
    whose Escape then waited out a quiet window for nothing. Its quiet empty prompt says
    where it is."""
    _row(project)
    scan.session, scan.tail, scan.state = None, None, "waiting"
    pane.screen = IDLE[3:]
    response = phone.post("agent/tell", agent=LABEL, text="read the task", mode="prompt")
    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True
    assert [(kind, what) for _pane, kind, what in pane.sent] == [
        ("paste", "read the task"),
        ("key", "Enter"),
    ]


@pytest.mark.parametrize(
    ("screen", "busy"),
    [
        pytest.param(IDLE, False, id="at-its-empty-prompt"),
        pytest.param(ESC_AGAIN, True, id="after-a-pad-esc"),
    ],
)
def test_interrupt_and_tell_at_a_codex_with_no_turn_to_stop_sends_no_escape(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
    screen: list[str],
    busy: bool,
) -> None:
    """Review r16 of #113, 2/8: Interrupt & tell sent its Escape whatever Codex's screen
    showed. After a pad Esc at its idle prompt (``esc again to edit previous message`` in its
    footer, the pane busy with that redraw), it was the second, which opens Codex's
    transcript, where Enter rewinds (measured on 0.162.1): the tell ended 409 ``still_busy``
    and left Codex there. At its empty prompt with no turn on screen there is nothing to
    stop, and a typed line and its Enter land there, the Esc footer's too (measured): the
    line is typed with no Escape."""
    _row(project, session="ses_codex")
    scan.session, scan.tail = _stopped_turn(project), ROLLOUT_UNREAD
    pane.screen, pane.busy, pane.after_escape = screen, busy, BROWSING
    response = phone.post("agent/tell", agent=LABEL, text="use the test DB", mode="interrupt")
    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True
    assert [(kind, what) for _pane, kind, what in pane.sent] == [
        ("paste", "use the test DB"),
        ("key", "Enter"),
    ]
    assert pane.screen == screen, "no transcript browser opened"


def test_interrupt_and_tell_mid_turn_still_stops_a_codex_turn_with_one_escape(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The control: a turn on screen (its status line above the empty prompt it keeps) is
    what the Escape is for. One goes, and the line is typed once the prompt is back."""
    _row(project, session="ses_codex")
    scan.session = _stopped_turn(project).model_copy(update={"state": "working"})
    scan.state, scan.tail, pane.screen, pane.busy = "working", ROLLOUT_UNREAD, WORKING, True
    pane.after_escape, pane.rests_on_escape = IDLE, True
    response = phone.post("agent/tell", agent=LABEL, text="use the test DB", mode="interrupt")
    assert response.status_code == 200, response.text
    assert [(kind, what) for _pane, kind, what in pane.sent] == [
        ("key", "Escape"),
        ("paste", "use the test DB"),
        ("key", "Enter"),
    ]


@pytest.mark.parametrize(
    ("screen", "why"),
    [
        (DRAFT, "the human's draft, which a typed line would join"),
        (BROWSING, "its transcript after a second Esc, where Enter rewinds"),
    ],
)
def test_a_codex_showing_anything_over_its_empty_prompt_takes_no_prompt_tell(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
    screen: list[str],
    why: str,
) -> None:
    _row(project, session="ses_codex")
    scan.session, scan.tail = _stopped_turn(project), ROLLOUT_UNREAD
    pane.screen = screen
    response = phone.post("agent/tell", agent=LABEL, text="carry on", mode="prompt")
    assert response.status_code == 409, why
    assert response.json()["error"] == "dialog_open"
    assert pane.sent == []


def test_a_codex_mid_turn_is_working_to_prompt_mode(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
) -> None:
    """A turn keeps Codex's empty prompt on screen, its status line redrawn every second:
    the pane is never quiet, so it is not at rest, whatever its screen's last line."""
    _row(project, session="ses_codex")
    scan.session = _stopped_turn(project).model_copy(update={"state": "working"})
    scan.state, scan.tail, pane.screen, pane.busy = "working", ROLLOUT_UNREAD, WORKING, True
    response = phone.post("agent/tell", agent=LABEL, text="carry on", mode="prompt")
    assert response.status_code == 409, response.text
    assert response.json() == {
        "error": "agent_busy",
        "message": "coder-1 is working — use Interrupt & tell",
    }
    assert pane.sent == []


def test_a_codex_whose_screen_tmux_cannot_show_is_one_nothing_reads(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Unknown is not an empty prompt: its board and transcript do not see the dialog Codex
    opens after a turn's ``Stop``. Prompt mode types nothing, and Interrupt & tell's Escape
    goes first, as for any agent nothing here reads."""
    _row(project, session="ses_codex")
    scan.session, scan.tail = _stopped_turn(project), ROLLOUT_UNREAD
    pane.unreadable = True
    response = phone.post("agent/tell", agent=LABEL, text="carry on", mode="prompt")
    assert response.status_code == 409, response.text
    assert response.json() == {
        "error": "agent_busy",
        "message": "coder-1's screen cannot be read, so nothing here shows whether it is at "
        "its prompt or showing one — use Interrupt & tell, whose Esc (No) goes first",
    }
    assert pane.sent == []
    assert scan.snaps[0].screen_unread and scan.snaps[0].screen is None


def test_a_claude_code_row_reads_no_screen(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The control: Claude Code's adapter reads no empty prompt off its screen, so its
    snapshot asks tmux nothing more, and it is judged as its own tests pin: a row with no
    board session is one nothing reads."""
    _row(project, agent="claude-code")
    pane.command, pane.screen = "claude", SWITCH_DIALOG
    response = phone.post("agent/tell", agent=LABEL, text="carry on", mode="prompt")
    assert response.status_code == 409
    assert response.json()["message"].startswith("coder-1 runs without aisquare's hooks")
    assert scan.snaps[0].screen is None


#: Codex's folder-trust question in a git checkout no trust covers, before any hook (0.162.1,
#: measured for task #108): Enter trusts the folder, Esc quits Codex.
TRUST_DIALOG = [
    "  Folder access",
    "  /work/repo",
    "",
    "  Trust this folder? Codex can read, edit, and run files here, subject to your permission",
    "  settings. Folder settings can run code automatically, even without a model request.",
    "  Continue only if you trust these files. Your trust decision will be saved.",
    "",
    f"{CHEVRON} 1. Trust and continue",
    "  2. Quit",
    "",
    "  enter continue · esc quit",
]


def test_an_auto_tell_at_a_fresh_codexs_folder_trust_dialog_sends_no_one_to_interrupt_and_tell(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    fleet: FleetCalls,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """SMOKE7 of #113, step A5: the auto tell filed a board note, which a Codex with no hook yet
    is not shown, and its receipt said "Interrupt & tell types it". Interrupt & tell refuses
    the dialog before its Escape, so it typed nothing there. The receipt says it types once
    the dialog is answered; an agent nothing reads at no dialog keeps the old advice
    (``test_an_auto_tell_to_an_agent_nothing_reads_is_a_board_note_and_never_fleet_tell``)."""
    _row(project)
    scan.session, scan.tail, scan.state = None, None, "waiting"
    pane.screen = TRUST_DIALOG

    response = phone.post("agent/tell", agent=LABEL, agent_id="agt_one", text="read the task")

    assert response.status_code == 200, response.text
    told = response.json()
    assert told["delivered"] is False
    assert told["how"].startswith("it is showing a prompt, which typing would answer — filed as")
    assert told["how"].endswith(
        "which it is not shown without aisquare's hooks: once its prompt is answered, "
        "Interrupt & tell types it"
    )
    assert fleet.calls == [] and pane.sent == []
    refused = phone.post("agent/tell", agent=LABEL, text="read the task", mode="interrupt")
    assert refused.status_code == 409 and refused.json()["error"] == "dialog_open"
    assert pane.sent == [], "no Escape: Esc quits Codex at this dialog"


# --- stop: the guard before its /exit and Enter ------------------------------------------------


def test_a_codex_at_its_dialog_is_not_stopped_through_it_and_one_at_rest_is(
    phone: Phone,  # noqa: F811
    scan: OneRowScan,
    pane: CodexPane,
    fleet: FleetCalls,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    _at_its_dialog(project, scan, pane)
    refused = phone.post("agent/stop", **PINNED)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == "dialog_open"
    assert fleet.calls == [] and pane.sent == []
    pane.screen = IDLE
    assert phone.post("agent/stop", **PINNED).status_code == 200
    assert fleet.names() == ["stop"] and pane.sent == [], "at rest: no Escape first"


# --- the key pad: each key as the agent reads it ----------------------------------------------


def _tap(phone: Phone, *keys: str) -> Any:  # noqa: F811
    return phone.post("send-keys", agent=LABEL, keys=list(keys))


def test_the_pads_new_line_reaches_a_codex_as_its_ctrl_j(
    phone: Phone,  # noqa: F811
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Codex 0.162.1 submits on ``S-Enter`` and takes ``C-j`` as its newline
    (``AgentAdapter.key_spellings``): the pad's ⇧⏎ sent as it is submitted the half-typed
    line. Enter is Enter, and the audit line keeps the names tapped."""
    _row(project)
    assert _tap(phone, "S-Enter").status_code == 200
    assert _tap(phone, "Enter").status_code == 200
    assert pane.keys() == ["C-j", "Enter"]
    assert "keys=[S-Enter]" in phone.audit()[0][1]


@pytest.mark.parametrize(
    ("version", "sent"),
    [((3, 7), "S-Enter"), ((3, 5), "S-Enter"), ((3, 4), "C-j"), (None, "C-j")],
)
def test_the_pads_new_line_reaches_claude_code_as_its_tmux_can_send_it(
    phone: Phone,  # noqa: F811
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
    version: tuple[int, int] | None,
    sent: str,
) -> None:
    """Claude Code reads ``S-Enter`` as its newline, which tmux below 3.5 types by its name:
    there it goes as ``C-j``, its newline on every tmux (``keys.LEGACY_FALLBACK``), and so
    where the server will not say its version."""
    _row(project, agent="claude-code")
    pane.command, pane.version = "claude", version
    assert _tap(phone, "S-Enter").status_code == 200
    assert pane.keys() == [sent]


def test_a_key_no_agent_spells_another_way_goes_as_tapped() -> None:
    """Only a key an agent spells otherwise, or one an old tmux types by name, asks the row's
    agent or the server's version."""

    class Unasked:
        def server_version(self) -> None:
            raise AssertionError("asked the server's version")

    class Row:
        @property
        def agent(self) -> str:
            raise AssertionError("asked the row's agent")

    keys = ["Enter", "Escape", "1", "C-c", "BTab", "Up"]
    spelled = remote_server.remote_keys_spelled(Unasked(), Row(), keys)  # type: ignore[arg-type]
    assert spelled == keys


def test_the_pad_offers_the_new_line_and_the_server_takes_it() -> None:
    page = (Path(__file__).resolve().parents[1] / "src/aisquare/web/remote/app.js").read_text(
        encoding="utf-8"
    )
    assert '["⇧⏎", "S-Enter"]' in page and '"S-Enter": "New line"' in page
    assert remote_server.check_remote_key_names(["S-Enter"]) == ["S-Enter"]
    assert "S-Enter" in remote_server.REMOTE_KEY_VOCABULARY


# --- the key pad: the one press that exits Codex ----------------------------------------------


@pytest.mark.parametrize("key", ["C-c", "C-d"])
@pytest.mark.parametrize(
    "screen",
    [
        pytest.param(IDLE, id="after-a-turn"),
        pytest.param(ESC_AGAIN, id="after-one-esc"),
        pytest.param(WORKING, id="mid-turn"),
    ],
)
def test_one_exit_key_to_a_codex_at_its_empty_prompt_is_sent_only_once_confirmed(
    phone: Phone,  # noqa: F811
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
    key: str,
    screen: list[str],
) -> None:
    """Measured on 0.162.1 under tmux 3.7c: one Ctrl-C at Codex's empty prompt exits it, a
    fresh one's, after a turn and after one Esc alike, and one Ctrl-D does with a turn running
    too (a Ctrl-C there opens its ``Cancel task`` / ``Exit`` choice). The double-press guard
    refused only a second press within 3 s, so an API caller's one key exited the agent,
    ended its row and woke the manager. It is refused now, nothing typed, naming the agent
    for the page's sheet, and sent once confirmed; a second press is still the double press."""
    _row(project)
    pane.screen = screen
    refused = _tap(phone, key)
    assert refused.status_code == 409, refused.text
    said = {"C-c": "Ctrl-C", "C-d": "Ctrl-D"}[key]
    reason = f"coder-1 is at Codex's empty prompt, where one {said} can exit it"
    assert refused.json() == {
        "error": "one_press_exit",
        "message": f"{reason} — send confirm_exit: true to mean it",
        "reason": reason,
    }
    assert pane.sent == []
    confirmed = phone.post("send-keys", agent=LABEL, keys=[key], confirm_exit=True)
    assert confirmed.status_code == 200, confirmed.text
    assert pane.keys() == [key]
    again = _tap(phone, key)
    assert again.status_code == 409 and again.json()["error"] == "double_press"
    assert pane.keys() == [key]


@pytest.mark.parametrize("key", ["C-c", "C-d"])
def test_an_exit_key_to_a_codex_over_its_draft_goes_as_a_first_press(
    phone: Phone,  # noqa: F811
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
    key: str,
) -> None:
    """Over a draft neither key ends Codex: its Ctrl-C clears the draft, its Ctrl-D does
    nothing (0.162.1). It goes unasked, as any agent's first press does."""
    _row(project)
    pane.screen = DRAFT
    response = _tap(phone, key)
    assert response.status_code == 200, response.text
    assert pane.keys() == [key]


def test_a_codex_exit_key_after_other_keys_in_its_body_is_asked_whatever_the_screen(
    phone: Phone,  # noqa: F811
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The screen is read before the body's keys: the keys ahead of the exit key may empty the
    draft it shows, and the Ctrl-C then lands at the empty prompt."""
    _row(project)
    pane.screen = DRAFT
    refused = _tap(phone, "C-u", "C-c")
    assert refused.status_code == 409, refused.text
    assert refused.json()["message"] == (
        "the keys before the Ctrl-C may leave coder-1 at Codex's empty prompt, where one "
        "Ctrl-C can exit it — send confirm_exit: true to mean it"
    )
    assert pane.sent == []


def test_a_codex_whose_screen_tmux_cannot_show_is_asked_before_its_exit_key(
    phone: Phone,  # noqa: F811
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Unknown is not a draft: nothing says the key would not land at its empty prompt."""
    _row(project)
    pane.unreadable = True
    refused = _tap(phone, "C-d")
    assert refused.status_code == 409, refused.text
    assert refused.json()["message"] == (
        "coder-1's screen could not be read to see whether Codex is at its empty prompt, where "
        "one Ctrl-D can exit it — send confirm_exit: true to mean it"
    )
    assert pane.sent == []


def test_a_claude_code_row_keeps_its_double_press_and_reads_no_screen(
    phone: Phone,  # noqa: F811
    pane: CodexPane,
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: Claude Code asks for a second press (``Press Ctrl-C again to exit``, and
    the same of Ctrl-D, measured on 2.1.304). Its first goes as it went, with no screen read,
    and a second within 3 s is the double press its own tests pin."""
    _row(project, agent="claude-code")
    pane.command, pane.screen = "claude", IDLE

    def unread(pane_id: str) -> list[str]:
        raise AssertionError("read Claude Code's screen")

    monkeypatch.setattr(pane, "screen_text", unread)
    assert _tap(phone, "C-c").status_code == 200
    second = _tap(phone, "C-d")
    assert second.status_code == 409 and second.json()["error"] == "double_press"
    assert pane.keys() == ["C-c"]


def test_only_a_key_some_agent_exits_on_asks_the_rows_agent() -> None:
    """Every other key goes as it went: neither the row's agent nor its screen is asked."""

    class Unread:
        def screen_text(self, pane_id: str) -> list[str]:
            raise AssertionError("read the screen")

    class Row:
        pane_id = "%1"

        @property
        def agent(self) -> str:
            raise AssertionError("asked the row's agent")

    keys = ["Enter", "Escape", "1", "BTab", "Up", "C-u", "C-l", "C-j"]
    exiting = remote_server.remote_one_press_exit(Unread(), Row(), keys, label=LABEL)  # type: ignore[arg-type]
    assert exiting is None


# --- Codex's screen: the empty prompt after an Esc --------------------------------------------


@pytest.mark.parametrize(
    ("screen", "idle"),
    [
        pytest.param(IDLE, True, id="empty-prompt"),
        pytest.param(ESC_AGAIN, True, id="after-one-esc"),
        pytest.param(BROWSING, False, id="after-two-esc"),
        pytest.param(DRAFT, False, id="draft"),
        pytest.param(SWITCH_DIALOG, False, id="dialog"),
        pytest.param(
            [f"{CHEVRON} Ask Codex to do anything", "  esc again to edit previous message"],
            True,
            id="inline-after-one-esc",
        ),
    ],
)
def test_codex_reads_its_prompt_after_one_esc_as_its_empty_prompt(
    screen: list[str], idle: bool
) -> None:
    """Measured on 0.162.1 under tmux: one Esc at the empty prompt of a session with a turn
    behind it puts ``esc again to edit previous message`` where ``? for shortcuts`` was, in
    its alternate screen, and in place of its footer under ``--no-alt-screen``, until the
    next key; a line typed there and its Enter went to the provider as a prompt. A second
    Esc opens its transcript. No hint is drawn over a draft."""
    read = screen_of(CODEX).idle_on_screen
    assert read is not None and read(screen) is idle


# --- the explainability card: the lane that traces the agent ----------------------------------

DEAD_PROXY = DoctorCheck(
    name="explainability proxy",
    status=CheckStatus.fail,
    detail="no proxy is listening at http://127.0.0.1:9090",
    fix="aisquare explainability proxy start",
)


@pytest.fixture
def tracing_on(
    fleet_home: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> ExplainabilitySettings:
    """Tracing and shipping on, to a target with a gateway and a key, the SDK importable, and
    no proxy listening, as on a machine whose fleet runs Codex alone."""
    monkeypatch.delenv(ops.TARGET_ENV_VAR, raising=False)
    monkeypatch.setenv(KEY_ENV_VAR, "aisq_test_key")
    config = load_config()
    config.explainability.enabled = True
    config.explainability.ship = True
    config.explainability.target = "stg"
    config.explainability.targets = {"stg": ExplainabilityTarget(gateway_url="https://stg.example")}
    save_config_file(config)
    monkeypatch.setattr(ops, "sdk_presence", lambda: ops.SdkPresence(True, None, "1.2.0", False))
    monkeypatch.setattr(ops, "_check_proxy", lambda *a, **k: DEAD_PROXY)
    return config.explainability


def _on(agent: str) -> None:
    """``fleet_home``'s row, spawned on ``agent`` (an upsert keeps a row's agent as it was)."""
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        store._conn.execute("UPDATE fleet_agent SET agent = ? WHERE id = 'agt_live'", (agent,))
        store._conn.commit()


SOURCE = "Codex's own telemetry, to a receiver each launch starts"


@pytest.mark.skipif(os.name != "posix", reason="Codex's telemetry receiver is POSIX-only")
def test_a_codex_agents_card_is_judged_by_its_own_telemetry_and_names_it(
    tracing_on: ExplainabilitySettings,
) -> None:
    """GET ``api/explainability/<agent>`` judged every agent by the doctor's checks, the
    Claude Code proxy's among them: the model proxy never carries Codex's traffic, which
    its own OpenTelemetry carries to a receiver each launch starts
    (``services.native_telemetry``), and the card said off for it."""
    _on("codex")
    card = remote_server.live_sources().explainability("coder-1", None)
    assert card["available"] is True and "reason" not in card, card
    policy = card["policy"]
    assert isinstance(policy, dict) and policy["source"] == SOURCE
    assert (policy["tracing"], policy["shipping"], policy["target"]) == (True, True, "stg")


def test_a_claude_code_agents_card_is_still_judged_by_the_proxy(
    tracing_on: ExplainabilitySettings,
) -> None:
    card = remote_server.live_sources().explainability("coder-1", None)
    assert card["available"] is False
    assert card["reason"] == f"doctor is RED: explainability proxy: {DEAD_PROXY.detail}"
    policy = card["policy"]
    assert isinstance(policy, dict) and "source" not in policy


def test_a_codex_agents_card_with_shipping_off_says_its_telemetry_is_not_carried(
    tracing_on: ExplainabilitySettings,
) -> None:
    config = load_config()
    config.explainability.ship = False
    save_config_file(config)
    _on("codex")
    card = remote_server.live_sources().explainability("coder-1", None)
    assert card["available"] is False
    assert card["reason"] == (
        "doctor is RED: explainability Codex telemetry: Codex exports its own telemetry to "
        "a receiver each launch starts, which runs only while insight shipping is on: its "
        "sessions run untraced"
    )


@pytest.mark.skipif(os.name == "posix", reason="the receiver runs here")
def test_a_codex_agents_card_off_posix_says_its_receiver_does_not_run(
    tracing_on: ExplainabilitySettings,
) -> None:
    _on("codex")
    card = remote_server.live_sources().explainability("coder-1", None)
    assert card["available"] is False
    assert "telemetry receiver runs on POSIX only" in str(card["reason"])


def test_each_lane_has_its_own_verdict_and_the_doctor_still_checks_the_proxy(
    tracing_on: ExplainabilitySettings,
) -> None:
    doctor = [check.name for check in ops.checks(tracing_on)]
    assert doctor[-1] == "explainability proxy"
    codex = [check.name for check in ops.checks(tracing_on, adapter=CODEX)]
    assert codex == [*doctor[:-1], "explainability Codex telemetry"]
    claude = [check.name for check in ops.checks(tracing_on, adapter=get_adapter("claude-code"))]
    assert claude == doctor
    remote_server._doctor_cache = None
    assert remote_server._doctor_verdict().red == [f"explainability proxy: {DEAD_PROXY.detail}"]
    assert remote_server._agent_verdict(CODEX).red == (
        []
        if os.name == "posix"
        else [
            "explainability Codex telemetry: Codex's telemetry receiver runs on POSIX only: its "
            "sessions run untraced"
        ]
    )


def test_an_agent_with_no_tracing_lane_is_red_once_tracing_is_on(
    tracing_on: ExplainabilitySettings,
) -> None:
    """An agent with neither the proxy (``model_proxy``) nor telemetry of its own
    (``native_telemetry``), as ``tests/fake_agent.py`` is: nothing traces its sessions, so its
    card is off and says why, and with tracing off that is only a warning."""
    from tests.fake_agent import FakeAgentAdapter

    fake = FakeAgentAdapter()
    (lane,) = [c for c in ops.checks(tracing_on, adapter=fake) if c.name.endswith(fake.label)]
    assert lane.status is CheckStatus.fail
    assert lane.detail == (
        "Fake Agent has no tracing lane: neither the proxy nor telemetry of its own carries "
        "its model traffic, so its sessions run untraced"
    )
    off = tracing_on.model_copy(update={"enabled": False})
    (lane,) = [c for c in ops.checks(off, adapter=fake) if c.name.endswith(fake.label)]
    assert lane.status is CheckStatus.warn
    assert remote_server._explainability_source(fake) is None
