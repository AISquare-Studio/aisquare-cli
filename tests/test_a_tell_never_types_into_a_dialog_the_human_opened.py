"""``fleet tell`` never types into a dialog a WAITING Claude Code agent shows (R6 F5).

Measured on Claude Code 2.1.318 (``tests/test_claude_native_transcript.py``): a human opens
``/resume`` (or ``/model``, ``/config``) at the idle prompt of a fleet agent, whose hooks
still say ``waiting``; ``fleet tell`` pasted its text into the picker and pressed Enter,
which there resumes another conversation or switches the model. main 0.9.2 did the same.
Now the agent's own screen reader decides: where it NAMES a dialog (``AgentScreen.modal``),
the tell is a board note whose words say what is open and that Esc closes it. A screen it
names nothing on is typed into as before, so no tell that typed before is lost: every
real idle shape of 2.1.318 (its empty box, a draft, after a turn, with and without the
fleet's named rule, 72 to 200 columns) still types. Each screen is a real capture, from
``tests/fixtures/agent_screens/claude-code.json``.

The same captures pin R6 F2: in a fleet window 2.1.318 leaves its box's named rule under
its permission chooser, which the reader now reads past, so the captain's ``press``
answers it (``prompt_showing``) and ``say`` names it.

R25: Claude Code 2.1.327 (2026-11-17) reworded its pickers' footers (``Esc to close``,
``Esc to go back``), and a short pane cuts a footer off, so ``/config`` and ``/resume`` read
as no dialog and a tell was pasted into their search box and sent with an Enter, said to be
typed. Every picker draws the same ``▔`` edge where the box was (``claude_screen.PANEL_EDGE``),
on 2.1.318 too, where ``/hooks``, and ``/model``, ``/status`` and ``/help`` at 50x22, read as no
dialog either. Their real captures, at 50x22 and 120x40, are read the same way here.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import screen_of
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux, _board_session, _coder, _events

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

SCREENS = Path(__file__).parent / "fixtures" / "agent_screens" / "claude-code.json"
VERSION = "Claude Code 2.1.318"
#: R25's: the Claude Code that reworded its pickers' footers.
NEW = "Claude Code 2.1.327"


def _captures(kind: str) -> dict[str, list[str]]:
    shown: list[dict[str, Any]] = json.loads(SCREENS.read_text(encoding="utf-8"))[kind]
    return {s["what"]: s["rows"] for s in shown if s["what"].startswith((VERSION, NEW))}


def _picker(what: str) -> str:
    """The slash command a capture's ``what`` names (``Claude Code 2.1.327's /config …``)."""
    return what.split("'s ")[1].split(" ")[0]


PICKERS = {
    what: rows
    for what, rows in {**_captures("dialogs"), **_captures("not_dialogs")}.items()
    if "picker" in what or "/config" in what
}
IDLE = {what: rows for what, rows in _captures("not_dialogs").items() if " idle " in what}
CHOOSERS = {what: rows for what, rows in _captures("dialogs").items() if "chooser" in what}


#: Every picker 2.1.327 opens at its idle prompt (R25), each captured at both sizes.
NEW_PICKERS = {
    "/config", "/resume", "/model", "/permissions", "/hooks", "/memory", "/status", "/theme",
    "/help",
}  # fmt: skip


def test_the_captures_cover_every_shape_the_ruling_names() -> None:
    """/resume, /model and /config; idle with a draft, after a turn, named and not, 72 to
    200 columns; the chooser in a fleet window at two widths and started by hand. And R25's:
    every picker 2.1.327 opens, at asq's 50x22 and at 120x40, and its idle screen at both."""
    old = {w: rows for w, rows in IDLE.items() if w.startswith(VERSION)}
    picked = {_picker(w) for w in PICKERS if w.startswith(VERSION)}
    assert picked >= {"/resume", "/model", "/config"}
    assert len(old) == 5 and any("draft" in w for w in old) and any("by hand" in w for w in old)
    assert {len(max(rows, key=len)) for rows in old.values()} >= {72, 120, 200}
    assert len(CHOOSERS) == 3
    for size in ("50x22", "120x40"):
        shown = {_picker(w) for w in PICKERS if w.startswith(NEW) and f" at {size} " in w}
        assert shown == NEW_PICKERS, size
        assert [w for w in IDLE if w.startswith(NEW) and f" at {size} " in w], size


@pytest.mark.parametrize("what", sorted(PICKERS))
def test_a_tell_to_a_waiting_agent_at_a_picker_is_a_note_that_says_how_to_close_it(
    what: str, tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")
    tmux.screens[agent.pane_id] = PICKERS[what]

    result = fleet_service.tell(project, "coder-1", "please rebase")

    assert not result.delivered and result.typed == "no", result
    assert tmux.typed == [], "nothing pasted, no Enter for the picker to take"
    named = screen_of(get_adapter("claude-code")).modal(PICKERS[what])
    assert named and f"its pane shows {named}" in result.how, result.how
    assert "close it with Esc in its pane" in result.how, result.how
    assert "board note" in result.how and _events(project, "note") == ["please rebase"]


@pytest.mark.parametrize("what", sorted(IDLE))
def test_a_tell_to_a_waiting_agent_at_any_real_idle_shape_still_types(
    what: str, tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")
    tmux.screens[agent.pane_id] = IDLE[what]

    result = fleet_service.tell(project, "coder-1", "please rebase")

    assert screen_of(get_adapter("claude-code")).modal(IDLE[what]) is None, "no dialog"
    if "draft" in what:
        # The human's draft is the draft guard's (P0-W2), as before: never typed over.
        assert result.typed == "no" and "draft" in result.how, result
        assert "its pane shows" not in result.how
        return
    assert result.typed != "no" and (agent.pane_id, "paste", "please rebase") in tmux.typed, result
    assert _events(project, "note") == []


@pytest.mark.parametrize("what", sorted(CHOOSERS))
def test_the_permission_chooser_reads_with_its_keys_in_a_fleet_window_and_by_hand(
    what: str,
) -> None:
    """F2: the captain's ``press`` reads 1 for Yes and Esc for No, and ``say`` names it,
    wherever 2.1.318 drew the chooser."""
    screen = screen_of(get_adapter("claude-code"))
    assert screen.screen_prompt is not None
    shown = screen.screen_prompt(CHOOSERS[what])
    assert shown is not None and (shown.shape, shown.yes_key, shown.no_key) == (
        "chooser",
        "1",
        "Escape",
    )
    assert shown.question == "Do you want to proceed?"
    assert screen.modal(CHOOSERS[what]) == "a numbered choice"
    assert not screen.ready(CHOOSERS[what])


# --- R25: a panel is read by the edge it draws where the box was ------------------------------

#: The captures no footer reads: cut off on their pane, or worded as no footer rule knew.
EDGE_ONLY = sorted(w for w in PICKERS if "footer cut off" in w or "no footer rule knew" in w)
#: The edge as 2.1.327 draws it over a 50-column pane, the effort label set in it.
EDGE = "▔" * 29 + " ◐ medium · /effort ▔"


def test_the_pickers_no_footer_reads_are_captured_on_both_versions_and_short_panes() -> None:
    assert any(w.startswith(NEW) and " at 50x22 " in w for w in EDGE_ONLY)
    assert any(w.startswith(NEW) and " at 50x12 " in w for w in EDGE_ONLY)
    assert any(w.startswith(VERSION) for w in EDGE_ONLY)


@pytest.mark.parametrize("what", EDGE_ONLY)
def test_a_picker_no_footer_reads_is_read_as_a_dialog_by_its_edge(what: str) -> None:
    screen = screen_of(get_adapter("claude-code"))
    rows = PICKERS[what]
    assert screen.modal(rows) == "a dialog waiting for Enter or Esc"
    without = [row for row in rows if not row.lstrip().startswith("▔")]
    assert len(without) == len(rows) - 1, "one edge"
    assert screen.modal(without) is None, "the edge is what reads it"


@pytest.mark.parametrize("what", sorted(w for w in PICKERS if w.startswith(NEW)))
def test_every_picker_of_claude_2_1_327_is_neither_ready_nor_its_box(what: str) -> None:
    """Nothing types into it: the fleet's ``ready`` and the captain's ``drawn`` both say no."""
    screen = screen_of(get_adapter("claude-code"))
    rows = PICKERS[what]
    assert screen.modal(rows) in ("a dialog waiting for Enter or Esc", "a numbered choice")
    assert not screen.ready(rows) and not screen.drawn(rows)


@pytest.mark.parametrize("what", sorted(IDLE))
def test_an_edge_above_a_drawn_box_is_no_dialog(what: str) -> None:
    """The edge is read only with no box drawn: a reply that quotes a panel (a capture pasted
    into the conversation) leaves an idle box ready and names nothing."""
    screen = screen_of(get_adapter("claude-code"))
    quoted = [EDGE, *IDLE[what]]
    assert screen.modal(quoted) is None
    assert screen.ready(quoted) == screen.ready(IDLE[what]) is True


# --- #87's N5: a start the board has not seen -------------------------------------------------

session_start_for_real = fleet_suite.session_start_for_real
#: Claude Code's folder-trust dialog as a real pane showed it (the 4b fixture's).
TRUST = next(
    s["rows"]
    for s in json.loads(SCREENS.read_text(encoding="utf-8"))["dialogs"]
    if s["what"] == "the real trust dialog"
)


def _not_started(project: ProjectInfo, tmux: FakeTmux, rows: list[str]) -> Any:
    """A Claude Code coder whose session the board names but whose SessionStart this launch
    never recorded (its hooks broken or missing, a session from before this launch's
    record), quiet, its pane showing ``rows``."""
    agent = _coder(project)
    assert agent.session_id is not None
    _board_session(agent, "waiting", hooked=False)
    tmux.set_command(agent.pane_id, "claude")
    tmux.printed(agent.pane_id, ago=timedelta(minutes=1))
    tmux.screens[agent.pane_id] = rows
    return agent


@pytest.mark.parametrize("what", sorted(what for what in IDLE if "draft" not in what))
def test_a_claude_agent_whose_start_was_not_seen_is_typed_into_at_its_real_idle_prompt(
    what: str,
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    session_start_for_real: None,
) -> None:
    """#87's N5 (ruled 11142): no hook said it started, but its own screen reads its idle
    prompt, so it is typed into as 0.9.2 did rather than only noted."""
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    agent = _not_started(project, tmux, IDLE[what])

    told = fleet_service.tell(project, agent.label, "please rebase")

    assert told.delivered and (agent.pane_id, "paste", "please rebase") in tmux.typed, told
    assert _events(project, "note") == []


def test_a_claude_agent_whose_start_was_not_seen_at_its_trust_dialog_gets_the_note(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    session_start_for_real: None,
) -> None:
    """The other side: its trust dialog is no prompt, and an Enter there exits it."""
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    agent = _not_started(project, tmux, TRUST)

    told = fleet_service.tell(project, agent.label, "please rebase")

    assert not told.delivered and "has not started" in told.how, told
    assert tmux.typed == [] and _events(project, "note") == ["please rebase"]
