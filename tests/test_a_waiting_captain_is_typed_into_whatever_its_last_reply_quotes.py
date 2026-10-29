"""A waiting captain takes a line at its drawn prompt, whatever its own last reply quotes.

Review r17 of #113, findings 1 and 9 (adb1c980). ``say`` and ``send`` came to ask
``ready_on_screen`` of every captain, a waiting one included. Those rows above the prompt are
the captain's own last reply, so one that quoted a busy footer (Claude Code's ``esc to
interrupt``, a ``· … …`` line; Codex's status line) read as a turn running: every later
line waited out the whole timeout and failed "never drew its prompt". And the settle meant
for a NEW prompt was set by "not ready", so a captain whose prompt was drawn but busy paid
``TYPE_SETTLE_S`` on every say once its turn ended (13294).

Waiting needs the prompt drawn alone (``prompt_on_screen``: Claude Code's input box, Codex's
empty composer); working needs it free as well (``ready_on_screen``). The settle goes only to
a prompt first seen undrawn.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aisquare.services.captain import brain
from tests.captain_screens import INPUT_BOX, REAL_WORKING
from tests.test_captain_say import Captain, Clock, captain  # noqa: F401 - fixture, by name
from tests.test_remote_agent_pane import _recorded

#: Claude Code: the captain's reply quotes a worker's busy footer, right above its box.
CLAUDE_QUOTES_ESC = [
    "⏺ coder-1 is stuck mid-turn: its footer still says esc to interrupt.",
    "",
    *INPUT_BOX,
]
#: Claude Code: the reply ends on a line shaped like its own spinner's.
CLAUDE_ENDS_ON_A_SPINNER_SHAPE = [
    "⏺ Here is the fleet:",
    "  · coder-1 · working on tsk_01m5 for 3m…",
    *INPUT_BOX,
]
#: What starts Codex's composer row and every prompt above it: U+203A and a space.
CHEVRON = "\u203a "
#: Codex: the reply's last row is shaped like its status line, right above its empty composer.
CODEX_QUOTES_ITS_STATUS_LINE = [
    f"{CHEVRON}how is coder-1?",
    "• coder-1 is stuck (3m 2s • esc to interrupt)",
    f"{CHEVRON}Ask Codex to do anything",
    "  local-fixture default · /work/repo",
    "  ? for shortcuts",
]
#: Codex mid-turn: its composer drawn and empty, its status line above it.
CODEX_WORKING = [
    f"{CHEVRON}SLOW one",
    "• Working (2s • esc to interrupt)",
    f"{CHEVRON}Ask Codex to do anything",
    "  local-fixture default · /work/repo",
    "  ? for shortcuts",
]


def _on(fake: Captain, agent: str) -> None:
    """The present captain, on ``agent``: its row and its launch record."""
    fake.present()  # type: ignore[attr-defined]
    assert fake.row is not None
    fake.row = _recorded(fake.row.model_copy(update={"agent": agent}))


@pytest.mark.parametrize(
    ("agent", "screen", "door"),
    [
        ("claude-code", CLAUDE_QUOTES_ESC, "say"),
        ("claude-code", CLAUDE_QUOTES_ESC, "send"),
        ("claude-code", CLAUDE_ENDS_ON_A_SPINNER_SHAPE, "say"),
        ("claude-code", CLAUDE_ENDS_ON_A_SPINNER_SHAPE, "send"),
        # send alone: this fake's transcript is Claude Code's, which say reads the reply from
        ("codex", CODEX_QUOTES_ITS_STATUS_LINE, "send"),
    ],
    ids=["claude-esc-say", "claude-esc-send", "claude-spinner-say", "claude-spinner-send",
         "codex-status-send"],
)  # fmt: skip
def test_a_waiting_captain_whose_reply_quotes_a_busy_footer_is_typed_into_at_once(
    captain: tuple[Captain, Clock],  # noqa: F811 - the fixture, by name
    agent: str,
    screen: list[str],
    door: str,
) -> None:
    fake, clock = captain
    _on(fake, agent)
    fake.screen = list(screen)
    asked_at = clock.now
    if door == "say":
        assert brain.say("what is up", timeout=60).text == "Nothing needs you right now."
    else:
        brain.send("what is up", timeout=60)
    assert fake.typed == [("paste", "what is up"), ("keys", "Enter")]
    assert fake.typed_at == asked_at, "typed at once: its prompt was drawn from the first read"


@pytest.mark.parametrize(
    ("agent", "busy"), [("claude-code", REAL_WORKING), ("codex", CODEX_WORKING)]
)
def test_a_captain_busy_at_its_drawn_prompt_is_typed_into_without_a_settle(
    captain: tuple[Captain, Clock],  # noqa: F811 - the fixture, by name
    agent: str,
    busy: list[str],
) -> None:
    """Its prompt drawn throughout, busy for 10 s: the line goes in at the first poll after
    the turn ends, with no settle (13294)."""
    fake, clock = captain
    _on(fake, agent)
    fake.busy_for = 10.0
    fake.busy_screen = list(busy)
    fake.screen = list(INPUT_BOX if agent == "claude-code" else CODEX_QUOTES_ITS_STATUS_LINE[2:])
    asked_at = clock.now
    brain.send("what is up", timeout=60)
    assert fake.typed == [("paste", "what is up"), ("keys", "Enter")]
    assert fake.typed_at is not None
    waited = fake.typed_at - asked_at
    assert waited < timedelta(seconds=10.0 + brain.TYPE_SETTLE_S), waited


@pytest.mark.parametrize(
    ("agent", "busy"), [("claude-code", REAL_WORKING), ("codex", CODEX_WORKING)]
)
def test_a_captain_the_fleet_reads_working_is_still_waited_out_at_a_busy_prompt(
    captain: tuple[Captain, Clock],  # noqa: F811 - the fixture, by name
    agent: str,
    busy: list[str],
) -> None:
    """The control: working, its prompt busy, nothing is typed."""
    fake, _ = captain
    _on(fake, agent)
    fake.state = "working"
    fake.screen = list(busy)
    with pytest.raises(brain.NoReply, match="stayed working for 5s"):
        brain.send("what is up", timeout=5)
    assert fake.typed == []
