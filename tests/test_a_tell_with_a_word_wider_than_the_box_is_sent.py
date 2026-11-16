"""A tell whose text holds a word wider than Claude Code's input box is sent, not read as a
human's draft beside ours (#95, the manager's finding on 2026-11-05).

The manager's `fleet tell` to a busy Claude Code coder answered "a draft appeared in its input
box while the text was being typed; ours is in the box with it, unsent", with nothing else in
the box. Reproduced on real Claude Code 2.1.318 (the loopback stub): Claude Code wraps a word
wider than its box inside the word (a long path, a URL), and the box's rows are read joined
with a space, so the render check read ``…-x -x…`` against our ``…-x-x…`` and refused. The
idle-return hint the pane showed (``new task? /clear to save 112.1k tokens``, above the box's
top rule) plays no part: the same tell is refused without it, and a short one types under it.
0.9.2 reads this capture the same way (its ``composer_text`` and ``holds`` are the head's); the
box is now compared with our text with the whitespace out.

CAPTURE is the bottom of that pane, escapes kept, as ``tmux capture-pane -e`` gave it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core.agent_adapters import claude_screen
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import delivery
from aisquare.services import fleet as fleet_service
from tests.captain_screens import MARK
from tests.test_delivery_guards import (  # noqa: F401 - fixtures, by name
    _box,
    _waiting,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)
from tests.test_fleet_service import FakeTmux

TOLD = (
    "see /Users/me/.aisquare/cache/claude-accounts/30/claude-501/"
    + "-x" * 70
    + "/scratchpad/r4_lines.md now"
)
"""What was told: a path wider than the 120-column pane's box."""

CAPTURE = [
    "                                                                                  "
    "\x1b[38;5;246mnew task? \x1b[38;5;153m/clear\x1b[38;5;246m to save \x1b[38;5;153m1.2k "
    "tokens\x1b[39m",
    "\x1b[38;5;244m" + "─" * 110 + " coder-1 ─",
    f"\x1b[39m{MARK}\xa0see /Users/me/.aisquare/cache/claude-accounts/30/claude-501/" + "-x" * 28,
    "  " + "-x" * 42 + "/scratchpad/r4_lines.md now\x1b[7m \x1b[0m",
    "\x1b[38;5;244m" + "─" * 120,
    "\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m",
]
"""Claude Code 2.1.318's pane once TOLD was pasted, under its idle-return hint."""


def test_the_box_holding_a_word_it_wrapped_holds_our_text() -> None:
    shown = claude_screen.composer_text(CAPTURE)
    assert shown is not None and "-x -x" in shown, "the box's rows are read joined by a space"
    assert delivery.holds(shown, TOLD)
    assert not delivery.holds(f"{shown} and a human's word", TOLD), "a draft beside it still is"
    assert not delivery.holds(f"a human's word {shown}", TOLD)


def test_a_tell_with_a_word_wider_than_the_box_is_sent(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coder = _waiting(tmux, project, role="coder")
    assert coder.session_id is not None
    real_paste, real_keys = tmux.paste, tmux.send_keys

    def paste(pane_id: str, text: str) -> None:
        real_paste(pane_id, text)
        tmux.screens[pane_id] = CAPTURE  # as Claude Code 2.1.318 drew it

    def submit(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        tmux.screens[pane_id] = _box()
        with store_session() as store:
            assert coder.session_id is not None
            store.mark_prompted(coder.session_id, datetime.now(tz=UTC) + timedelta(seconds=1))

    monkeypatch.setattr(tmux, "paste", paste)
    monkeypatch.setattr(tmux, "send_keys", submit)

    told = fleet_service.tell(project, coder.label, TOLD)

    assert told.typed == "yes", told.how
    assert tmux.typed == [(coder.pane_id, "paste", TOLD), (coder.pane_id, "key", "Enter")]
