"""P0-W2 (#276): nothing the fleet types lands in a human's draft, and a note to a busy
agent reaches it at the end of its turn.

- **The draft guard.** Every typed write asks ``delivery.draft_guard`` first: a manager's
  nudge, ``fleet tell``, a typed first message, the Remote's paste. Nothing goes in while
  Claude Code's input box holds a draft — read by structure, its own dim placeholder and
  suggestion left out — or while someone has typed into the pane in ``asq`` within
  ``[fleet] draft_quiet_seconds`` (the typing lease ``asq``'s ``_forward`` touches). A
  nudge a draft held back waits in ``team_meta`` and goes out once the box is clear; a
  tell is filed as a note and says why.
- **Turn-end delivery.** A non-manager's ``Stop`` hands it the notes addressed to it (its
  label or its role) past its cursor, as the manager's wake-up hands it the board, under
  the same guards: ``stop_hook_active``, the hourly cap, the cursor.

Driven as ``tests/test_fleet_service.py`` drives the fleet (the real store in an isolated
home, real git under ``tmp_path``, that suite's ``FakeTmux``), with Claude Code's input box
drawn the way W0 captured it on 2.1.309.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core import claude_screen
from aisquare.core.store import store_session
from aisquare.core.tmux import Completed, TmuxServer
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import delivery
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.captain import screen as captain_screen
from aisquare.services.fleet import NUDGE_TEXT
from tests import captain_screens
from tests import test_fleet_service as fleet_suite
from tests.captain_screens import MARK, REAL_RULE, REAL_TOP_RULE
from tests.test_fleet_service import FakeTmux, _board_session

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
typed_first_lines = fleet_suite.typed_first_lines


def _box(composer: str = "", *, dim: str = "") -> list[str]:
    """Claude Code 2.1.309's idle screen as W0 captured it: the input box with ``composer`` in
    it, or Claude Code's own dim words (SGR 2) after the mark and its no-break space."""
    shown = f"\x1b[2m{dim}\x1b[0m" if dim else composer
    return [
        "✻ Cooked for 2s · done",
        REAL_TOP_RULE,
        f"\x1b[39m{MARK}\xa0{shown}",
        REAL_RULE,
        "  ⏵⏵ auto mode on (shift+tab to cycle)",
    ]


DRAFT = "hey manager, about the auth task — I think we shou"


def _waiting(tmux: FakeTmux, project: ProjectInfo, role: str = "manager") -> FleetAgent:
    agent = fleet_service.spawn(project, role, worktree=False).agent
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")
    tmux.screens[agent.pane_id] = _box()
    return agent


def _deferred(manager: FleetAgent) -> str | None:
    with store_session() as store:
        return store.get_meta(f"{fleet_service.NUDGE_DEFERRED}{manager.session_id}") or None


# --- the composer reader, moved to core --------------------------------------------------------


def test_the_screen_reader_lives_in_core_and_the_captain_reads_the_same_one() -> None:
    """``core.claude_screen`` is Claude Code's adapter's reader
    (``core.agent_adapters.claude_screen``) under the name the fleet's door imports, and the
    captain's ``say``/``press`` ask a Claude Code pane's adapter for that same reader
    (``services.captain.screen``, by the pane's own agent, #113): one parser, never two that
    could disagree."""
    from aisquare.core.agent_adapters import DEFAULT_AGENT, get_adapter
    from aisquare.core.agent_adapters import claude_screen as adapters_screen
    from aisquare.core.agent_adapters.types import screen_of

    for name in ("composer_text", "input_box_at", "modal_showing", "box_idle", "prompt_showing"):
        assert getattr(claude_screen, name) is getattr(adapters_screen, name), name
    read = screen_of(get_adapter(DEFAULT_AGENT))
    assert read.screen_prompt is claude_screen.prompt_showing
    assert read.ready_on_screen is claude_screen.box_idle
    assert read.modal_on_screen is claude_screen.modal_showing
    assert captain_screen.prompt_showing.__module__ == "aisquare.services.captain.screen"


def test_the_composer_reads_a_box_on_every_captured_screen_and_nothing_else() -> None:
    """Over every capture the captain's readers are pinned on: a box is ``""`` (or a draft),
    and a screen with no box drawn — a chooser, the trust question, a turn quoting one — has
    no composer at all, so nothing reads a dialog as a draft or a draft as a dialog."""
    screens = {
        name: value
        for name, value in vars(captain_screens).items()
        if name.isupper() and isinstance(value, list) and all(isinstance(v, str) for v in value)
    }
    assert len(screens) >= 10
    for name, lines in screens.items():
        boxed = claude_screen.input_box_at(claude_screen.tail(lines)) is not None
        composer = claude_screen.composer_text(lines)
        assert (composer is not None) == boxed, name
        if boxed:
            assert composer == "", f"{name}: an empty box read as a draft ({composer!r})"


# --- the nudge ---------------------------------------------------------------------------------


def test_a_nudge_never_types_into_a_draft(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """#276: a human halfway through a message to the manager in asq had the nudge appended
    to it, and the Enter sent both. With a draft in the box, nothing is typed: the draft is
    intact, and the nudge waits."""
    manager = _waiting(tmux, project)
    tmux.screens[manager.pane_id] = _box(DRAFT)

    assert fleet_service.nudge_manager(project.id, reason="task_done") is False

    assert tmux.typed == [], "the draft is intact"
    assert _deferred(manager) is not None, "the nudge waits for the box to clear"


def test_a_dim_placeholder_is_not_a_draft(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Claude Code's own words in an empty box (W0's captures) are not a human's: the nudge
    goes in. Read after the escapes were stripped, they held every nudge back for good."""
    manager = _waiting(tmux, project)
    tmux.screens[manager.pane_id] = _box(dim="check the board again")

    assert fleet_service.nudge_manager(project.id, reason="task_done") is True
    assert (manager.pane_id, "paste", NUDGE_TEXT) in tmux.typed
    assert _deferred(manager) is None


def test_a_deferred_nudge_goes_out_once_the_composer_is_empty(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """asq's refresh retries a nudge a draft held back: while the draft is there, nothing;
    once the human clears the box, the nudge goes out once, and nothing is held back any
    more."""
    manager = _waiting(tmux, project)
    tmux.screens[manager.pane_id] = _box(DRAFT)
    assert fleet_service.nudge_manager(project.id, reason="task_done") is False

    assert fleet_service.retry_deferred_nudges() == 0, "the draft is still there"
    assert tmux.typed == []

    tmux.screens[manager.pane_id] = _box()  # the human cleared it
    assert fleet_service.retry_deferred_nudges() == 1
    assert (manager.pane_id, "paste", NUDGE_TEXT) in tmux.typed
    assert _deferred(manager) is None
    assert fleet_service.retry_deferred_nudges() == 0, "sent once"


def test_a_deferred_nudge_is_dropped_once_the_human_sent_the_draft(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Nothing is lost while it waits: the human's own Enter runs ``UserPromptSubmit``, which
    hands the manager the delta the nudge was for. So a nudge whose manager prompted since it
    was held back is dropped, not typed after the fact."""
    manager = _waiting(tmux, project)
    tmux.screens[manager.pane_id] = _box(DRAFT)
    assert fleet_service.nudge_manager(project.id, reason="task_done") is False
    assert manager.session_id is not None
    with store_session() as store:
        store.mark_prompted(manager.session_id, datetime.now(tz=UTC) + timedelta(seconds=1))
    tmux.screens[manager.pane_id] = _box()

    assert fleet_service.retry_deferred_nudges() == 0
    assert tmux.typed == [] and _deferred(manager) is None


def test_a_nudge_left_in_the_box_is_sent_not_typed_twice(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nudge whose Enter was dropped sits in the box as the nudge itself, not a human's
    draft: the next nudge sends it with an Enter, and never pastes a second copy beside it."""
    manager = _waiting(tmux, project)
    tmux.screens[manager.pane_id] = _box(NUDGE_TEXT)
    real_keys = tmux.send_keys

    def submit(pane_id: str, *keys: str) -> None:  # this time Claude Code takes it
        real_keys(pane_id, *keys)
        tmux.screens[pane_id] = _box()
        with store_session() as store:
            assert manager.session_id is not None
            store.mark_prompted(manager.session_id, datetime.now(tz=UTC) + timedelta(seconds=1))

    monkeypatch.setattr(tmux, "send_keys", submit)

    assert fleet_service.nudge_manager(project.id, reason="task_done") is True
    assert tmux.typed[0] == (manager.pane_id, "key", "Enter")
    assert all(kind != "paste" for _pane, kind, _text in tmux.typed)


# --- the typing lease ---------------------------------------------------------------------------


def test_a_human_typing_in_asq_holds_every_write_until_it_goes_quiet(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The second signal: a key typed into the pane in asq within ``draft_quiet_seconds``
    holds a nudge and a tell back even where the box reader sees nothing (a draft wrapped
    past the box, a box mid-redraw). Past the quiet window, they go in."""
    manager = _waiting(tmux, project)
    delivery.touch_typing_lease(tmux, manager.pane_id)

    assert fleet_service.nudge_manager(project.id, reason="task_done") is False
    told = fleet_service.tell(project, manager.label, "status?")
    assert told.typed == "no" and "someone is typing in its pane" in told.how
    assert tmux.typed == []

    quiet = time.time() - fleet_service.settings().draft_quiet_seconds - 1
    lease = delivery.typing_lease(tmux, manager.pane_id)
    os.utime(lease, (quiet, quiet))
    assert fleet_service.nudge_manager(project.id, reason="task_done") is True


def test_the_pane_touches_the_typing_lease_at_most_once_a_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_forward`` is the ONE way input reaches the agent from asq, so it is where the lease
    is touched — throttled: a key reaches the screen in a millisecond, a file touch per key
    would not. Another pane is touched at once."""
    from aisquare.cli.ui import terminal

    touched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        delivery, "touch_typing_lease", lambda server, pane: touched.append((server.socket, pane))
    )
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])  # the clock `_typing` reads
    pane = object.__new__(terminal.TerminalPane)
    pane._leased = None
    server = TmuxServer("asq-test", runner=lambda argv, stdin: None)  # type: ignore[arg-type, return-value]

    for _ in range(5):
        pane._typing(server, "%3")
    now[0] += 1.1
    pane._typing(server, "%3")
    pane._typing(server, "%4")

    assert touched == [("asq-test", "%3"), ("asq-test", "%3"), ("asq-test", "%4")]


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX only: no tmux socket path, so the lease is the name"
)
def test_the_typing_lease_is_the_servers_not_every_server_of_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #337, minor: every fleet server is named ``asq``, and every server numbers
    its panes from ``%0``. Leased by the name, a key typed into ``%1`` on one held every write
    to ``%1`` on another ``asq`` (another ``TMUX_TMPDIR``, a scratch fleet). The lease is the
    server's, by its socket's path: the same server asked by name (``-L``) or at its path
    (``-S``, a recorded row) is one lease, through the ``/tmp`` link macOS has."""
    from aisquare.cli.ui import terminal

    def no_screen(argv: Sequence[str], stdin: bytes | None) -> Completed:
        return Completed(1, "", "can't find pane: %1")

    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "real"))
    (tmp_path / "linked").symlink_to(tmp_path / "real")
    here = TmuxServer("asq", runner=no_screen)
    elsewhere = here.at(tmp_path / "other" / "tmux-0" / "asq")
    pane = object.__new__(terminal.TerminalPane)
    pane._leased = None

    pane._typing(here, "%1")

    typing = "someone is typing in its pane"
    assert delivery.draft_guard(here, "%1", quiet=10.0) == typing
    assert delivery.draft_guard(here.at(here.socket_path()), "%1", quiet=10.0) == typing
    through_link = tmp_path / "linked" / here.socket_path().relative_to(tmp_path / "real")
    assert delivery.draft_guard(here.at(through_link), "%1", quiet=10.0) == typing
    assert delivery.draft_guard(elsewhere, "%1", quiet=10.0) is None
    assert delivery.draft_guard(here, "%2", quiet=10.0) is None


# --- the tell ------------------------------------------------------------------------------------


def test_a_tell_never_types_into_a_draft_and_says_so(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A waiting coder with a human's draft in its box: the tell is not typed over it. It is
    filed as a note to the coder — which reaches it at the end of its next turn, or with its
    next prompt — and says why."""
    coder = _waiting(tmux, project, role="coder")
    tmux.screens[coder.pane_id] = _box("note to self: check the")

    told = fleet_service.tell(project, coder.label, "rebase onto main first")

    assert told.typed == "no" and tmux.typed == []
    assert "a draft is in its input box, which nothing types over" in told.how
    assert "note" in told.how
    with store_session() as store:
        notes = [e for e in store.events_since(project.id, 0, limit=200) if e.kind == "note"]
    assert [(n.text, n.to_role) for n in notes] == [("rebase onto main first", coder.label)]


def test_a_draft_typed_between_the_guard_and_the_paste_is_never_sent(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #337: the guard read an empty box, a human typed before the paste landed, and
    the box read ``half a thought <our text>``. The render check never saw our text alone,
    ran out, and fell through to Enter, which sent the human's draft with ours. Now nothing
    is pressed: the human's words stay in the box, and the tell is filed as a note. It says
    so as it happened: ours was typed beside the draft, and nothing was sent — not the
    guard's "which nothing types over", which is said before any paste."""
    coder = _waiting(tmux, project, role="coder")
    human = "half a thought"
    real_paste = tmux.paste

    def paste_after_a_human(pane_id: str, text: str) -> None:
        real_paste(pane_id, text)
        tmux.screens[pane_id] = _box(f"{human} {text}")  # their keys landed first

    monkeypatch.setattr(tmux, "paste", paste_after_a_human)
    told = fleet_service.tell(project, coder.label, "rebase onto main first")

    assert told.typed == "no"
    assert told.how.startswith(
        "a draft appeared in its input box while the text was being typed; ours is in the box "
        "with it, unsent, and nothing was sent — "
    )
    assert "nothing types over" not in told.how and "note" in told.how
    assert tmux.typed == [(coder.pane_id, "paste", "rebase onto main first")], "no Enter"
    shown = claude_screen.composer_text(tmux.screens[coder.pane_id])
    assert shown == f"{human} rebase onto main first", "the human's draft is intact"
    with store_session() as store:
        notes = [e for e in store.events_since(project.id, 0, limit=200) if e.kind == "note"]
    assert [(n.text, n.to_role) for n in notes] == [("rebase onto main first", coder.label)]


def test_a_first_prompt_a_draft_raced_says_it_was_not_sent_not_that_it_was_not_typed(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same race on a typed first message: no Enter, the human's words intact, and the
    receipt's note says the prompt is in the box with the draft, NOT sent — never "NOT typed
    … which nothing types over", which would have it typed in again beside the first."""
    original = tmux.pane_facts

    def up_at_once(pane_id: str) -> object:
        tmux.set_command(pane_id, "claude")
        return original(pane_id)

    real_paste = tmux.paste

    def paste_after_a_human(pane_id: str, text: str) -> None:
        real_paste(pane_id, text)
        tmux.screens[pane_id] = _box(f"half a thought {text}")

    monkeypatch.setattr(tmux, "pane_facts", up_at_once)
    monkeypatch.setattr(tmux, "paste", paste_after_a_human)
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="start on tsk_1")
    pane = receipt.agent.pane_id

    assert receipt.prompt_typed == "no"
    assert tmux.typed == [(pane, "paste", "start on tsk_1")], "no Enter"
    assert claude_screen.composer_text(tmux.screens[pane]) == "half a thought start on tsk_1"
    (note,) = [n for n in receipt.notes if "draft" in n]
    assert note.startswith(
        "the prompt was NOT sent: a draft appeared in its input box while the prompt was being "
        "typed; ours is in the box with it, unsent, and nothing was sent. Read its input box "
        "before you send it again: "
    )
    assert "NOT typed" not in note and "nothing types over" not in note


def test_a_tell_to_an_idle_agent_types_and_is_acknowledged(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control, and the door's other half: an empty box, the text pasted and sent,
    and the agent's ``UserPromptSubmit`` the acknowledgement."""
    coder = _waiting(tmux, project, role="coder")
    assert coder.session_id is not None
    real_keys = tmux.send_keys

    def submit(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        tmux.screens[pane_id] = _box()
        with store_session() as store:
            assert coder.session_id is not None
            store.mark_prompted(coder.session_id, datetime.now(tz=UTC) + timedelta(seconds=1))

    monkeypatch.setattr(tmux, "send_keys", submit)
    told = fleet_service.tell(project, coder.label, "rebase onto main first")

    assert told.typed == "yes"
    assert tmux.typed == [
        (coder.pane_id, "paste", "rebase onto main first"),
        (coder.pane_id, "key", "Enter"),
    ]


# --- turn-end delivery ----------------------------------------------------------------------------


def _session(project: ProjectInfo, session_id: str, role: str) -> TeamSession:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        return store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role=role,
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(project.id),
            )
        )


def _note(project: ProjectInfo, text: str, *, to: str | None, kind: str = "note") -> None:
    team_service.add_note(text, kind=kind, to_role=to, session_ref=None, cwd=project.root)


def test_a_note_to_an_idle_coder_is_delivered_at_its_turn_end(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """#276: a note to a busy coder waited for a prompt that might never come — a coder
    whose turn ends is given nothing. Now its ``Stop`` keeps it going on the note, as the
    manager's wake-up does: the delta as the reason, the cursor past it."""
    coder = fleet_service.spawn(project, "coder", worktree=False).agent
    assert coder.session_id is not None
    before = _session(project, coder.session_id, "coder")
    _note(project, "rebase onto main before you push", to=coder.label)

    decision = team_service.hook_stop(coder.session_id, project.root)

    assert decision is not None and "rebase onto main before you push" in decision.reason
    assert f"You are {coder.label}: what is addressed to {coder.label} or to coder" in (
        decision.reason
    ), "it is told who it is: live, a coder read a note to its own label as someone else's"
    assert decision.reason.endswith(team_service.WAKEUP_CLOSE)
    with store_session() as store:
        after = store.get_session(coder.session_id)
    assert after is not None and after.cursor > before.cursor and after.state == "working"


def test_a_note_to_its_role_reaches_it_too(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = fleet_service.spawn(project, "coder", worktree=False).agent
    assert coder.session_id is not None
    _session(project, coder.session_id, "coder")
    _note(project, "every coder: the API moved to v2", to="coder", kind="decision")

    decision = team_service.hook_stop(coder.session_id, project.root)
    assert decision is not None and "the API moved to v2" in decision.reason


@pytest.mark.parametrize(
    ("to", "kind"),
    [(None, "note"), ("reviewer", "note"), ("coder", "task_added")],
    ids=["unaddressed", "someone-else", "a-task-for-the-role"],
)
def test_nothing_else_keeps_an_agent_going(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, to: str | None, kind: str
) -> None:
    """The control: only a NOTE ADDRESSED to it hands an agent its board at the end of a turn.
    A task added for its role names the role too, and the pool reaches it through its cycle."""
    coder = fleet_service.spawn(project, "coder", worktree=False).agent
    assert coder.session_id is not None
    before = _session(project, coder.session_id, "coder")
    with store_session() as store:
        team_service._emit(store, project.id, kind, "something", to_role=to)

    assert team_service.hook_stop(coder.session_id, project.root) is None
    with store_session() as store:
        after = store.get_session(coder.session_id)
    assert after is not None and after.state == "waiting" and after.cursor == before.cursor


def test_turn_end_delivery_respects_stop_hook_active_and_the_cap(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It never loops: a Stop already continuing on a stop hook waits, and so does one past
    ``[fleet] max_continuations_per_hour``; the cursor stays, so the note is the next
    prompt's. Off with ``[fleet] deliver_notes_at_turn_end = false``."""
    coder = fleet_service.spawn(project, "coder", worktree=False).agent
    assert coder.session_id is not None
    _session(project, coder.session_id, "coder")
    _note(project, "rebase first", to=coder.label)

    assert team_service.hook_stop(coder.session_id, project.root, stop_hook_active=True) is None
    with monkeypatch.context() as off:
        off.setattr(team_service, "deliver_notes_at_turn_end", lambda: False)
        assert team_service.hook_stop(coder.session_id, project.root) is None
    with monkeypatch.context() as capped:
        capped.setattr(team_service, "continuation_cap", lambda: 0)
        assert team_service.hook_stop(coder.session_id, project.root) is None
    assert team_service.hook_stop(coder.session_id, project.root) is not None, "then it goes"


@pytest.mark.parametrize(
    ("context", "verdict_shown"),
    [("none", False), ("relevant", False), ("all", True)],
)
def test_turn_end_delivery_reads_the_board_through_the_agents_board_context(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    context: str,
    verdict_shown: bool,
) -> None:
    """Sync #365 (P0-W2 x P1 WS-2): a note to a busy reviewer reaches it at its turn's end,
    and nothing more of the board than its board context lets its prompt's delta read. A
    cold reviewer (``none``, the reviewer default) was handed another reviewer's verdict
    with the note; ``relevant`` keeps what is addressed to it; ``all`` is the board."""
    reviewer = fleet_service.spawn(project, "reviewer", worktree=False).agent
    assert reviewer.session_id is not None
    _session(project, reviewer.session_id, "reviewer")
    other = _session(project, "eeee5555-0000-0000-0000-000000000000", "coder")
    team_service.add_note("PASS #12: verified", kind="result", session_ref=other.id)
    _note(project, "please look at #361 next", to=reviewer.label)
    monkeypatch.setenv("AISQUARE_BOARD_CONTEXT", context)

    decision = team_service.hook_stop(reviewer.session_id, project.root)

    assert decision is not None and "please look at #361 next" in decision.reason
    assert ("PASS #12: verified" in decision.reason) is verdict_shown, decision.reason


def test_a_note_to_a_cold_reviewer_behind_a_page_of_others_still_reaches_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #365, round 2: under ``none`` the addressed notes are asked for in the
    query. Filtered after its page (31 events), a note behind 40 other verdicts never woke
    the reviewer, and its prompts read no board, so the note was lost."""
    reviewer = fleet_service.spawn(project, "reviewer", worktree=False).agent
    assert reviewer.session_id is not None
    _session(project, reviewer.session_id, "reviewer")
    other = _session(project, "eeee5555-0000-0000-0000-000000000000", "coder")
    for n in range(40):
        team_service.add_note(f"PASS #{n}: verified", kind="result", session_ref=other.id)
    _note(project, "please look at #361 next", to=reviewer.label)
    monkeypatch.setenv("AISQUARE_BOARD_CONTEXT", "none")

    decision = team_service.hook_stop(reviewer.session_id, project.root)

    assert decision is not None and "please look at #361 next" in decision.reason
    assert "PASS #" not in decision.reason
