"""Remote's needs-you card for a Codex agent at a dialog of its own that no hook reports (task
#108, found by SMOKE7 of #113).

Codex 0.162.1 asks ``Trust this folder?`` in a git checkout no trust covers and reviews its
hooks before its first prompt, and opens ``Switch to gpt-6-luna for lower credit usage?``
after a turn's ``Stop`` near its limit: none fires a hook, so the row read ``waiting`` with
no bell, and the scan had no card and no push, while Remote's guard refused every tell as a
dialog. Each test goes through the store, the fleet's listing and the live sources, as the
phone's scan does, over the screens measured on 0.162.1 (``tests/fixtures/agent_screens/
codex.json``). A Claude Code row is the control: its screen is never read for this.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core.store import store_session
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession
from aisquare.services import remote_needs, remote_push
from aisquare.services.remote_needs import needs_item_id, scan_needs_you
from aisquare.services.remote_server import Runtime, build_app
from tests.remote_kit_helpers import base, make_client, unlock
from tests.test_remote_needs import FakeTmux
from tests.test_remote_needs_codex import (  # noqa: F401 - fixtures, by name
    SCREENS,
    _codex_agent,
    _moment,
    _stamp,
    project,
    runtime,
    tmux,
)

TRUST = SCREENS["folder trust, asked in a git checkout no trust covers"]
TRUST_WORDS = (
    "Trust this folder? Codex can read, edit, and run files here, subject to your permission "
    "settings. Folder settings can run code automatically, even without a model request. "
    "Continue only if you trust these files. Your trust decision will be saved."
)
#: The same screen as the scan's capture gives it (``capture-pane -e``), its chosen option and
#: its key hint styled as measured.
TRUST_STYLED = [
    *(f"\x1b[1;7m{row}" if row.startswith("\u203a 1.") else row for row in TRUST[:-1]),
    "  \x1b[1menter\x1b[0;2m continue · \x1b[0;1mesc\x1b[0;2m quit\x1b[0m",
]
REVIEW = SCREENS["the hook review"]
REVIEW_MOVED = SCREENS["the hook review, its highlight moved by a 2"]
SWITCH = SCREENS["Switch to gpt-6-luna, after a turn's Stop near the limit"]
IDLE = SCREENS["its empty prompt after the Switch dialog's Esc"]
GRACE = timedelta(seconds=30)


def _fresh(project: ProjectInfo, *, agent: str = "codex", now: datetime) -> FleetAgent:  # noqa: F811
    """A fleet row spawned 20 s ago that no hook has reported: no board session (``no hooks``
    in ``fleet ls``), as a Codex is until its first prompt."""
    row = FleetAgent(
        id="agt_t5", project_id=project.id, label="t5", role="coder", agent=agent,
        binary="codex" if agent == "codex" else "claude", pane_id="%1", cwd=project.root,
        created_at=now - timedelta(seconds=20),
    )  # fmt: skip
    with store_session() as store:
        store.upsert_fleet_agent(row)
    return row


def _cards(
    now: datetime, first_seen: dict[str, datetime] | None = None
) -> list[remote_needs.NeedsItem]:
    return scan_needs_you(
        remote_needs.live_needs_sources(), now=now, dismissed=(), first_seen=first_seen
    )


# --- before the first prompt: the folder-trust question and the hook review --------------------


@pytest.mark.parametrize("captured", [TRUST, TRUST_STYLED], ids=["plain", "styled"])
def test_a_fresh_codex_at_its_folder_trust_question_is_a_card_pushed_after_a_grace(
    project: ProjectInfo,  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
    captured: list[str],
) -> None:
    """The tester's step A: ``fleet ls`` read ``t5 coder waiting … no hooks``, and the scan made
    nothing of it. Its card names who and no agent, quotes the question, offers no quick
    answer (Enter trusts the folder, Esc quits Codex: the key pad beside the live screen
    answers it), and is pushed 30 s after the scan first saw it, as Claude Code's question
    is after its start."""
    now = datetime.now(UTC)
    row = _fresh(project, now=now)
    tmux.screen = list(captured)
    seen: dict[str, datetime] = {}

    (card,) = _cards(now, seen)

    assert (card.kind, card.agent, card.reason) == (
        "permission",
        "t5",
        "t5 shows a dialog that needs you",
    )
    assert card.excerpt == remote_needs._needs_excerpt(TRUST_WORDS)
    assert card.detail == {"text": TRUST_WORDS}
    assert card.answers == ()
    assert "Claude" not in card.reason + card.excerpt
    assert (card.since, card.push_after) == (now, now + GRACE)
    subject = f"screen:{row.id}:-:{remote_needs._needs_squashed(TRUST_WORDS)}"
    assert card.id == needs_item_id(project.id, "permission", subject)
    later = _cards(now + timedelta(seconds=12), seen)
    assert [(c.id, c.since, c.push_after) for c in later] == [(card.id, now, now + GRACE)]


def test_its_card_as_the_phone_reads_it_and_its_push_name_no_agent(
    tmp_path: Path,
    project: ProjectInfo,  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
    runtime: Runtime,  # noqa: F811
) -> None:
    """``GET api/needs`` had no item for t5, and no push would go."""
    now = datetime.now(UTC)
    _fresh(project, now=now)
    tmux.screen = list(TRUST)
    client = make_client(build_app(runtime, dist_dir=tmp_path))
    assert unlock(client, runtime).status_code == 200

    (shown,) = client.get(f"{base(runtime)}/api/needs").json()["items"]

    assert (shown["kind"], shown["agent"], shown["answers"]) == ("permission", "t5", [])
    assert shown["reason"] == "t5 shows a dialog that needs you"
    assert shown["excerpt"].startswith("Trust this folder? Codex can read, edit, and run files")
    (card,) = _cards(now)
    pushed = remote_push.push_needs_message([card], total=1, base_url=None)
    assert (pushed["title"], pushed["body"]) == (
        f"{card.project_name}: t5 needs you",
        "t5 shows a dialog that needs you",
    )


def test_the_hook_review_after_it_is_a_card_of_its_own_whatever_its_highlight(
    project: ProjectInfo,  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """Trusted, Codex reviews its hooks, as quietly and with no hook either: the next dialog,
    its own card. A ``2`` there moves the highlight and picks nothing (measured): the same
    dialog, the same card. Nor does a narrower pane, which rewraps the question."""
    now = datetime.now(UTC)
    _fresh(project, now=now)
    tmux.screen = list(TRUST)
    (trusted,) = _cards(now)
    narrow = [f"  {row}" for row in textwrap.wrap(TRUST_WORDS, 58)]
    tmux.screen = [*TRUST[:3], *narrow, *TRUST[6:]]
    assert [card.id for card in _cards(now)] == [trusted.id]

    tmux.screen = list(REVIEW)
    (review,) = _cards(now)
    assert review.reason == "t5 shows a dialog that needs you"
    assert review.excerpt == (
        "Hooks need review 8 hooks are new or changed. "
        "Hooks can run outside the sandbox after you trust them."
    )
    assert review.id != trusted.id
    tmux.screen = list(REVIEW_MOVED)
    assert [card.id for card in _cards(now)] == [review.id]


@pytest.mark.parametrize(
    "what",
    [
        "its empty prompt after a turn",
        "its empty prompt after one Esc",
        "its transcript after a second Esc",
        "a draft",
        "a draft that starts 1. ",
        "a turn running",
    ],
)
def test_a_codex_showing_no_dialog_of_its_own_is_no_card(
    project: ProjectInfo,  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
    what: str,
) -> None:
    """The control: its empty prompt, the human's own draft (one that starts like a dialog's
    first option too), its transcript browser and a turn are nothing that needs the human,
    though the guard refuses a tell into the draft or the browser."""
    now = datetime.now(UTC)
    _fresh(project, now=now)
    tmux.screen = list(SCREENS[what])
    assert _cards(now) == []


def test_a_claude_code_row_has_its_screen_read_for_no_such_card(
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """Claude Code's dialogs reach the board through its ``Notification`` hook, and its
    folder-trust question has a card of its own (``fleet.TRUST_PROMPT``): its adapter reads
    no dialog off its screen, so the scan captures none for one, as before."""
    now = datetime.now(UTC)
    _fresh(project, agent="claude-code", now=now)
    tmux.command, tmux.screen = "claude", list(SWITCH)
    read: list[str] = []
    screen = remote_needs._needs_screen_read

    def needs_screen_read(status: FleetAgentStatus) -> list[str]:
        read.append(status.agent.label)
        return screen(status)

    monkeypatch.setattr(remote_needs, "_needs_screen_read", needs_screen_read)

    assert _cards(now) == []
    assert read == [], "the scan's own capture (the fleet's trust check captures it apart)"


# --- after a turn's Stop: Switch to gpt-6-luna ------------------------------------------------


def _stopped(tmp_path: Path, project: ProjectInfo, now: datetime) -> datetime:  # noqa: F811
    """A Codex whose turn ended on a question (its last words ``… Shall I go on?``), its Stop
    hook's ``waiting`` 20 s ago."""
    rollout, records = _moment(tmp_path, "asked", now - timedelta(seconds=21))
    stopped = _stamp(records[-1]) + timedelta(milliseconds=200)
    _codex_agent(
        project, state="waiting", seen=stopped, born=now - timedelta(hours=1), rollout=rollout
    )
    return stopped


def test_the_switch_dialog_after_a_stop_is_its_card_in_place_of_the_asked_one(
    tmp_path: Path,
    project: ProjectInfo,  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """The tester's step B: for 8 minutes ``api/needs`` had nothing for d4 at the dialog. Its
    turn's last words asked a question, so the card was ``asked``, whose reply is words and
    an Enter typed at the prompt (``needs_prompt_now`` holds no words to a dialog): into the
    dialog, where Enter picks option 1 and switches the agent's model. While the dialog is
    up, it is the card; once Esc closes it, the question's card is back."""
    now = datetime.now(UTC)
    _stopped(tmp_path, project, now)
    tmux.screen = list(SWITCH)

    (card,) = _cards(now)

    assert (card.kind, card.reason) == ("permission", "coder-1 shows a dialog that needs you")
    assert card.excerpt == "Approaching rate limits Switch to gpt-6-luna for lower credit usage?"
    assert card.answers == () and card.push_after == now + GRACE
    tmux.screen = list(IDLE)
    (asked,) = _cards(now)
    assert (asked.kind, asked.reason) == ("asked", "coder-1 ended its turn with a question")


def test_the_same_dialog_after_another_turn_is_a_card_of_its_own(
    tmp_path: Path,
    project: ProjectInfo,  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """The dialog opens after each turn near the limit, word for word: named by the Stop
    before it, each is pushed, and a dismissal hides that one alone."""
    now = datetime.now(UTC)
    stopped = _stopped(tmp_path, project, now)
    tmux.screen = list(SWITCH)
    (first,) = _cards(now)
    with store_session() as store:
        session = store.get_session("ses_coder-1")
        assert isinstance(session, TeamSession)
        store.upsert_session(session.model_copy(update={"last_seen_at": stopped + GRACE}))

    (second,) = _cards(now + GRACE)

    assert second.excerpt == first.excerpt and second.id != first.id


def test_a_plan_proposed_in_plan_mode_is_its_implement_dialogs_card(
    tmp_path: Path,
    project: ProjectInfo,  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """Codex's ``Implement this plan?`` opens after a Plan-mode turn's ``Stop`` and leaves no
    record (the turn's ``task_complete`` is its rollout's last word), so a proposed plan was
    no card at all: no ``plan`` card, as for Claude Code's ``ExitPlanMode``, and none for the
    dialog. It is that dialog's card now, the plan above it on the screen the card opens."""
    now = datetime.now(UTC)
    rollout, records = _moment(tmp_path, "planned", now - timedelta(seconds=21))
    _codex_agent(
        project, state="waiting", seen=_stamp(records[-1]), born=now - timedelta(hours=1),
        rollout=rollout,
    )  # fmt: skip
    assert _cards(now) == [], "at its empty prompt, the plan is no card"
    tmux.screen = list(SCREENS["Implement this plan?, after a Plan-mode turn's Stop"])

    (card,) = _cards(now)

    assert (card.kind, card.excerpt, card.answers) == ("permission", "Implement this plan?", ())
