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
  on its box (a hand-started ``aisquare launch``) and in a fleet window (F2, a strict xfail),
  and the ``/resume`` picker, a dialog nothing types into.
- The npm install's version (F4, a strict xfail).
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


def _steered_session(on: Native) -> tuple[Any, Path]:
    """A coder's session with an edit, an interruption and a denied permission, then a last
    reply: its row and its transcript, once its last turn has ended."""
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
    facts, offset = reader.facts(transcript, 0)
    kinds = [fact.kind for fact in facts]
    assert kinds.count(FACT_PROMPT) == 4, facts
    assert [f.detail for f in facts if f.kind == FACT_EDIT] == [
        str(native.workspace / "notes.txt")
    ], facts
    assert kinds.count(FACT_INTERRUPT) == 1, facts
    assert kinds.count(FACT_DENIAL) == 1, facts
    assert offset == transcript.stat().st_size
    again_facts, again_offset = reader.facts(transcript, offset)
    assert (list(again_facts), again_offset) == ([], offset), "nothing found twice"
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
    native.until(lambda: (native.workspace / "asked.txt").exists(), 20, "approved", "hand")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "F2 (board seq 11071), pre-existing on 0.9.2: with --name, Claude Code 2.1.318 "
        "leaves its named box rule on the pane's last row under a dialog, and "
        "claude_screen.dialog_region reads only what follows the last rule"
    ),
)
@options(permission_mode="manual")
def test_real_claude_code_permission_chooser_in_a_fleet_window_reads_as_a_prompt(
    native: Native,  # noqa: F811
) -> None:
    receipt = native.spawn(prompt="ask me [[bash touch asked.txt]]")
    _reads_the_chooser(_chooser_on(native, receipt.agent.pane_id))


def _at_the_resume_picker(on: Native) -> tuple[str, list[str]]:
    """A coder whose turn ended, with the human's ``/resume`` picker open in its pane."""
    receipt = on.spawn(prompt="a conversation to list")
    pane = receipt.agent.pane_id
    on.heard("a conversation to list")
    on.until(lambda: on.state(receipt.agent.session_id) == "waiting", 30, "idle", pane)
    on.ready(pane)
    on.type(pane, "/resume")
    on.until(lambda: "Resume session" in on.screen(pane), 20, "the picker", pane)
    time.sleep(0.5)
    return receipt.agent.label, on.rows(pane)


@options()
def test_real_claude_code_resume_picker_reads_as_a_dialog_not_ready_for_a_line(
    native: Native,  # noqa: F811
) -> None:
    _, rows = _at_the_resume_picker(native)
    reader = screen_of(get_adapter(AGENT))
    assert reader.ready_on_screen is not None and not reader.ready_on_screen(rows)
    assert reader.prompt_on_screen is not None and not reader.prompt_on_screen(rows)
    assert reader.modal_on_screen is not None
    assert reader.modal_on_screen(rows) == "a dialog waiting for Enter or Esc", rows


@pytest.mark.xfail(
    strict=True,
    reason=(
        "F5 (board seq 11092), pre-existing on 0.9.2: fleet.tell's waiting branch pastes "
        "into a Claude Code pane with no input box drawn and presses Enter once"
    ),
)
@options()
def test_real_claude_code_at_its_resume_picker_is_told_through_the_board(
    native: Native,  # noqa: F811
) -> None:
    label, _ = _at_the_resume_picker(native)
    told = asq_json(native, "fleet", "tell", label, "not into the picker")
    assert told["typed"] == "no", told


@pytest.mark.xfail(
    strict=True,
    reason=(
        "F4 (board seq 11090), pre-existing on 0.9.2: npm links claude to the package's "
        "bin/claude.exe, and layout_version reads only bin/package.json"
    ),
)
@options()
def test_real_claude_code_npm_install_names_its_version(
    native: Native,  # noqa: F811
) -> None:
    package = Path(native.binary).resolve().parents[1] / "package.json"
    if not package.is_file():
        pytest.skip(f"{native.binary} is not an npm install")
    version = json.loads(package.read_text(encoding="utf-8"))["version"]
    assert get_adapter(AGENT).layout_version(native.binary) == version
