"""What aisquare reads off a real Claude Code's transcript and screen.

Run ``AISQUARE_TEST_CLAUDE=1 pytest tests/test_claude_native_transcript.py``, in the pattern of
``tests/test_claude_native.py`` (its loopback Messages stub and its ``native`` fixture).

- One real session holds an edit (a ``Write`` its settings allow), an interruption (Esc while
  the model's answer streams) and a denied permission (Esc at its chooser), then a last reply. The
  adapter's transcript reader reads it as the board and ``aisquare outcomes`` do: the facts of
  who steered, the token usage (every request once, a second scan adding nothing) and the
  ledger, which agree with the stub's own count of requests and with what the Stop hooks
  recorded; its tail and the agent's last words; the conversation and its opening.
- The screen reader on real dialogs: the permission chooser where Claude Code draws no name
  on its box (a hand-started ``aisquare launch``) and in a fleet window, whose named rule
  2.1.318 leaves under it, where the captain's ``press`` answers it (R6 F2); the ``/resume``,
  ``/model`` and ``/config`` pickers, which a ``fleet tell`` never types into (R6 F5).
- The npm install's version, read off the package's native build (R6 F4).
"""

from __future__ import annotations

import json
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import selfcli
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import (
    FACT_DENIAL,
    FACT_EDIT,
    FACT_INTERRUPT,
    FACT_PROMPT,
    screen_of,
)
from aisquare.core.store import store_session
from aisquare.services.captain import actions
from tests.test_claude_native import (  # noqa: F401 - `native` and `native_options` are fixtures
    AGENT,
    HEARD,
    Native,
    Stub,
    native,
    native_options,
    options,
    pytestmark,
)
from tests.test_claude_native_accounts import asq_global
from tests.test_claude_native_fleet import asq_json


def _usage_recorded(session_id: str, requests: int, seconds: float = 20) -> None:
    """Until the board's usage row for ``session_id`` counts ``requests`` requests, or
    ``seconds`` pass: a test's own assertions then say what the row holds."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with store_session() as store:
            recorded = store.session_usage(session_id)
        models = recorded.usage.get("models", {}) if recorded is not None else {}
        if sum(counts.get("requests", 0) for counts in models.values()) >= requests:
            return
        time.sleep(0.1)


def _steered_session(on: Native) -> tuple[Any, Path]:
    """A coder's session with an edit, an interruption and a denied permission, then a last
    reply: its row and its transcript, once its last turn has ended and its Stop hook has
    recorded the turn's usage."""
    receipt = on.spawn(prompt=f"edit it [[write {on.workspace / 'notes.txt'} hello]]")
    row = receipt.agent
    pane = row.pane_id
    on.until(lambda: (on.workspace / "notes.txt").exists(), 30, "the edit", pane)
    on.until(lambda: on.state(row.session_id) == "waiting", 30, "its Stop", pane)
    on.ready(pane)
    on.type(pane, "answer slowly [[stall]]")
    on.until(on.stub.holding.is_set, 30, "its answer streaming", pane)
    on.until(
        lambda: "stalled" in on.screen(pane) or "esc to interrupt" in on.screen(pane), 20, "x", pane
    )
    time.sleep(1)
    on.srv.send_keys(pane, "Escape")  # interrupted mid-answer
    on.until(lambda: "Interrupted" in on.screen(pane), 20, "interrupted", pane)
    on.stub.release()
    on.ready(pane)
    on.type(pane, "deny it [[bash touch denied.txt]]")
    on.until(lambda: on.state(row.session_id) == "attention", 30, "its chooser", pane)
    time.sleep(0.5)
    on.srv.send_keys(pane, "Escape")  # No
    on.until(lambda: "Interrupted" in on.screen(pane).rsplit("deny it", 1)[-1], 20, "no", pane)
    on.ready(pane)
    on.type(pane, "last words [[say the end]]")
    on.heard("last words")
    on.until(lambda: on.state(row.session_id) == "waiting", 30, "its last Stop", pane)
    # The Stop hook writes the row's state first and then reads the turn's usage onto the
    # board, in the same hook, as 0.9.2's did (`hooks.turn_stopped`). The row says waiting
    # before that record is in: 7-30 ms later on a laptop (2.1.318), and on a busy runner
    # late enough that the board was read first (CI run 42284663137 read 2 of the 5 requests).
    _usage_recorded(row.session_id, len(on.stub.turns))
    session = on.session(row.session_id)
    assert session is not None and session.transcript_path
    return row, Path(session.transcript_path)


@options(permission_mode="manual", allow=("Write",))
def test_real_claude_code_transcript_reads_its_steering_usage_ledger_and_outcomes(
    native: Native,  # noqa: F811
) -> None:
    row, transcript = _steered_session(native)
    reader = get_adapter(AGENT).transcript_reader
    assert reader is not None
    # Who steered: four prompts that opened turns, the edit, the interruption, the denial.
    # Claude Code appends its own bookkeeping (cost-state, last-prompt) after the last Stop,
    # so the file may grow under the read: the offset is at or past the size before it.
    before = transcript.stat().st_size
    facts, offset = reader.facts(transcript, 0)
    kinds = [fact.kind for fact in facts]
    assert kinds.count(FACT_PROMPT) == 4, facts
    assert [f.detail for f in facts if f.kind == FACT_EDIT] == [
        str(native.workspace / "notes.txt")
    ], facts
    assert kinds.count(FACT_INTERRUPT) == 1, facts
    assert kinds.count(FACT_DENIAL) == 1, facts
    assert before <= offset <= transcript.stat().st_size
    again_facts, _ = reader.facts(transcript, offset)
    assert list(again_facts) == [], "nothing found twice"
    # Its usage: each request the model answered, once, at the stub's own counts.
    answered = len(native.stub.turns)
    scan = reader.usage(transcript, None)
    assert scan is not None
    totals = scan.delta.totals()
    assert scan.delta.requests == answered, (scan.delta.models, answered)
    assert totals.input == Stub.INPUT_TOKENS * answered, totals
    # Every answer's output once; the interrupted one's as far as its message_start said.
    assert totals.output == Stub.OUTPUT_TOKENS * (answered - 1) + 1, totals
    assert totals.priced and totals.cost_usd > 0, totals
    assert scan.delta.tools.get("Write") == 1 and scan.delta.tools.get("Bash") == 1
    assert scan.first_edit.edited
    again = reader.usage(transcript, scan.state)
    assert again is not None and again.delta.is_empty(), "a second scan adds nothing"
    # The ledger counts the same requests; `metrics ledger` prints it for the session.
    ledger = reader.ledger(transcript, since=None, until=None)
    assert ledger.requests == answered, ledger
    printed = asq_global("--json", "metrics", "ledger", "--session", row.session_id)
    [line] = printed["sessions"]
    assert (line["session_id"], line["transcript_found"]) == (row.session_id, True), line
    assert (line["requests"], line["input_total"], line["output"]) == (
        answered,
        totals.input,
        totals.output,
    ), line
    assert line["tool_calls"] == {"Bash": 1, "Write": 1}, line
    # What the Stop hooks recorded on the board, each turn's end reading on from the last,
    # is what the reader reads: the interrupted and the denied turns, which ended with no
    # Stop, are counted at the next one.
    with store_session() as store:
        recorded = store.session_usage(row.session_id)
    assert recorded is not None and recorded.agent == AGENT
    [(model, kept)] = recorded.usage["models"].items()
    assert model == next(iter(scan.delta.models)), recorded.usage
    assert (kept["requests"], kept["input"], kept["output"]) == (
        answered,
        totals.input,
        totals.output,
    ), kept
    assert recorded.usage["first_edit"]["edited"] is True
    # Its tail and the agent's last words.
    tail = reader.tail_facts(transcript, transcript.stat().st_size, 1 << 20, datetime.now(UTC))
    assert tail.pending == () and tail.last_text == f"{HEARD}: the end", tail
    assert reader.reply_since(transcript, None) == f"{HEARD}: the end"
    # The conversation, as `fleet adopt` lists it, and its opening.
    listed = dict(reader.conversations(native.config_dir))
    assert listed.get(row.session_id) == transcript, listed
    folder, first = reader.opening(transcript)
    assert folder == str(native.workspace) and first and first.startswith("edit it"), first
    # `aisquare outcomes`: this week's direction counts the interruption and the denial.
    report = asq_global("--json", "outcomes", "--no-gh")
    direction, effort = report["direction"], report["effort"]
    assert (direction["interrupts"], direction["denied_permissions"]) == (1, 1), direction
    assert direction["human_turns"] == 4 and direction["agent_turns"] == 0, direction
    assert direction["by_origin"] == {"human:spawn": 1, "human:typed": 3}, direction
    assert (effort["sessions"], effort["files_edited"], effort["tool_calls"]) == (1, 1, 2)
    assert effort["tokens"]["input"] == totals.input, effort


# --- the screen reader on real dialogs ------------------------------------------------------


def _chooser_on(on: Native, pane: str) -> list[str]:
    on.until(lambda: "Do you want to proceed?" in on.screen(pane), 30, "chooser", pane)
    time.sleep(0.5)
    return on.rows(pane)


def _reads_the_chooser(rows: list[str]) -> None:
    reader = screen_of(get_adapter(AGENT))
    assert reader.screen_prompt is not None and reader.modal_on_screen is not None
    assert reader.ready_on_screen is not None and not reader.ready_on_screen(rows)
    shown = reader.screen_prompt(rows)
    assert shown is not None and shown.shape == "chooser", "\n".join(rows)
    assert (shown.yes_key, shown.no_key) == ("1", "Escape")
    assert shown.question == "Do you want to proceed?"
    assert reader.modal_on_screen(rows) == "a numbered choice"


@options(permission_mode="manual")
def test_real_claude_code_permission_chooser_reads_as_a_prompt_with_its_keys(
    native: Native,  # noqa: F811
) -> None:
    """Started by hand (``aisquare launch coder``, no ``--name``): its chooser is read with
    ``1`` for Yes and Esc for No, a modal, not ready for a typed line."""
    command = shlex.join(
        [
            *selfcli.argv_for(["launch", "coder"]),
            "--",
            "--permission-mode",
            "manual",
            "ask me [[bash touch asked.txt]]",
        ]
    )
    native.srv.run(
        "new-session", "-d", "-s", "hand", "-x", "120", "-y", "40",
        "-c", str(native.workspace), command,
    )  # fmt: skip
    _reads_the_chooser(_chooser_on(native, "hand"))
    native.srv.send_keys("hand", "1")  # what the captain's `press yes` sends
    assert native.until(lambda: (native.workspace / "asked.txt").exists(), 20, "yes", "hand")


@options(permission_mode="manual")
def test_real_claude_code_permission_chooser_in_a_fleet_window_is_answered_by_the_captain(
    native: Native,  # noqa: F811
) -> None:
    """R6 F2 (board seq 11071, ruled 11072): in a fleet window 2.1.318 leaves its box's
    named rule under the chooser; its reader reads past it, and the captain's ``press yes``
    approves the command, its ``press no`` refuses the next one."""
    receipt = native.spawn(prompt=f"ask me [[bash touch {native.workspace / 'yes.txt'}]]")
    label, pane = receipt.agent.label, receipt.agent.pane_id
    rows = _chooser_on(native, pane)
    assert native.until(lambda: native.screen(pane).rstrip().endswith(f"{label} ─"), 5, "rule")
    _reads_the_chooser(rows)
    yes = json.loads(actions.press(native.project.id, label, "yes", utterance="press yes"))
    assert (yes["sent"], yes["answered"]) == ("1", True), yes
    native.until(lambda: (native.workspace / "yes.txt").exists(), 20, "approved", pane)
    native.until(lambda: native.state(receipt.agent.session_id) == "waiting", 30, "idle", pane)
    native.ready(pane)
    native.type(pane, f"and this [[bash touch {native.workspace / 'no.txt'}]]")
    _chooser_on(native, pane)
    native.until(lambda: native.state(receipt.agent.session_id) == "attention", 30, "bell", pane)
    no = json.loads(actions.press(native.project.id, label, "no", utterance="press no"))
    assert (no["sent"], no["answered"]) == ("Escape", True), no
    native.until(lambda: "Do you want to proceed?" not in native.screen(pane), 20, "gone", pane)
    time.sleep(1)
    assert not (native.workspace / "no.txt").exists(), "a No runs nothing"


#: Claude Code's pickers a human opens at its idle prompt, and the words each one shows.
PICKERS = {"/resume": "Resume session", "/model": "Select model", "/config": "Esc to clear"}


def _at_a_picker(on: Native, picker: str) -> tuple[str, str, list[str]]:
    """A coder whose turn ended, with the human's ``picker`` open in its pane."""
    receipt = on.spawn(prompt="a conversation to list")
    pane = receipt.agent.pane_id
    on.heard("a conversation to list")
    on.until(lambda: on.state(receipt.agent.session_id) == "waiting", 30, "idle", pane)
    on.ready(pane)
    on.type(pane, picker)
    on.until(lambda: PICKERS[picker] in on.screen(pane), 20, "the picker", pane)
    time.sleep(0.5)
    return receipt.agent.label, pane, on.rows(pane)


@pytest.mark.parametrize("picker", sorted(PICKERS))
@options()
def test_real_claude_code_at_a_picker_is_told_through_the_board_and_how_to_close_it(
    native: Native,  # noqa: F811
    picker: str,
) -> None:
    """R6 F5 (board seq 11092, ruled 11093): its hooks say waiting, its screen shows a
    picker the human opened, where an Enter would act (a resume, a model switch). The
    reader names it a dialog, nothing is typed, the tell is a board note that says what is
    open and that Esc closes it; once closed, a tell types again."""
    label, pane, rows = _at_a_picker(native, picker)
    reader = screen_of(get_adapter(AGENT))
    assert reader.ready_on_screen is not None and not reader.ready_on_screen(rows)
    named = reader.modal(rows)
    assert named in ("a dialog waiting for Enter or Esc", "a numbered choice"), rows
    told = asq_json(native, "fleet", "tell", label, "not into the picker")
    assert told["typed"] == "no", told
    assert f"its pane shows {named}" in told["how"] and "close it with Esc" in told["how"]
    assert "not into the picker" not in native.screen(pane)
    for _ in range(3):  # /config's first Esc clears its search, the next closes it
        native.srv.send_keys(pane, "Escape")
        time.sleep(1)
        if reader.ready(native.rows(pane)):
            break
    native.ready(pane)
    again = asq_json(native, "fleet", "tell", label, "now at its prompt")
    assert again["typed"] == "yes", again
    native.heard("now at its prompt")


@options()
def test_real_claude_code_npm_install_names_its_version(
    native: Native,  # noqa: F811
) -> None:
    """R6 F4 (board seq 11090, ruled 11091): 2.x's npm build links ``claude`` to the
    package's ``bin/claude.exe``; the version is read from the package's own manifest."""
    package = Path(native.binary).resolve().parents[1] / "package.json"
    if not package.is_file():
        pytest.skip(f"{native.binary} is not an npm install")
    version = json.loads(package.read_text(encoding="utf-8"))["version"]
    assert get_adapter(AGENT).layout_version(native.binary) == version
    install = asq_global("--json", "accounts", "list")["claude"]
    assert install["version"] == version, install
