"""Whose prompt a rollout holds, for a first message nothing has spoken for (#24, tester-24).

A rollout is named for the time it began and holds the folder, and nothing of the process that
wrote it; Codex makes none until its first prompt runs (none after 33 s at a quiet prompt,
0.164.0). So a row's own is born with the prompt in question, and another Codex session's,
begun in the same folder on the same account home, reads the same. With the agent's hooks not
trusted nothing else takes an ``unsent`` row, and the listing took it for any prompt the folder's
rollouts gained: another agent's task hid a message that was never taken (the agent idle at its
prompt, its manager waiting). The process that writes a rollout holds it open, and it is below
the pane's own (``core.process_table``; measured on 0.164.0), so:

- a rollout the row's pane holds is the row's own, and nothing else is;
- where the process table cannot be read (``PromptRecord.own`` false) the message's own words
  still take the row, and no other prompt does unless this is the only live row in its folder.

Both are here with a made-up process table (``tests.test_codex_first_message_never_taken.
Processes``); ``tests/test_process_table.py`` reads real processes and
``tests/test_codex_native_295_296.py`` real Codex agents.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.agent_adapters import codex_prompts
from aisquare.core.process_table import PaneProcesses
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from tests import test_codex_first_message_never_taken as first
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_first_message_never_taken import (
    MESSAGE,
    PANE_PID,
    WORKING,
    Processes,
    Spawned,
    _unsent,
)
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    clock,
    project,
    repo,
    tmux,
)

OTHERS_TASK = "a different agent's task"
#: This module's fixtures are that one's, under the same names.
spawned = first.spawned
processes = first.processes


def _soon() -> datetime:
    return datetime.now(tz=UTC) + timedelta(seconds=50)


def _second(
    spawned: Spawned,
    tmux: FakeTmux,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    processes: Processes,
    monkeypatch: pytest.MonkeyPatch,
    *,
    message: str = "the second agent's own task",
) -> Spawned:
    """Another Codex row in the same folder, on the same home, its own pane and its own agent
    below it; not idle (a turn running), so that it stays ``pending`` until its rollout says so."""
    other = Spawned(project, tmux, monkeypatch, processes, message=message, pane_pid=PANE_PID + 10)
    assert other.agent.label == "coder-2" and other.cwd == spawned.cwd
    tmux.screens[other.agent.pane_id] = WORKING
    return other


# --- the tester's two repros


def test_a_prompt_another_agent_ran_in_the_folder_does_not_take_an_unsent_row(
    spawned: Spawned,
) -> None:
    """Repro 2 (unit): the row read ``unsent``, its own agent ran nothing, and another session
    in the folder on the same home ran a task. The agent idles at its empty prompt and its
    message is lost: the row must keep saying so, listing after listing."""
    _unsent(spawned)
    spawned.rollout([OTHERS_TASK], began=_soon(), held=False)

    for _ in range(3):
        status = spawned.listed()
        assert status.state == "attention"
        assert status.agent.first_prompt == "unsent"
    with store_session() as store:
        assert store.get_meta(f"{fleet_service.FIRST_PROMPT_META}{spawned.agent.id}") is not None


def test_two_rows_in_one_folder_each_take_only_the_prompts_their_own_agent_ran(
    spawned: Spawned,
    tmux: FakeTmux,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    processes: Processes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tester's trace of one listing, ``coder-1 unsent -> acked`` and ``coder-2 pending ->
    acked``, on one agent's prompt: coder-2's task ran; coder-1's stays unsent."""
    other = _second(spawned, tmux, project, processes, monkeypatch)
    _unsent(spawned)
    assert other.row().first_prompt == "pending"

    other.rollout(["the second agent's own task"], began=_soon())

    assert other.listed().state != "attention"
    assert other.row().first_prompt == "acked"
    assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"


def test_the_rows_own_tell_takes_it_whatever_another_agent_runs_beside_it(
    spawned: Spawned,
) -> None:
    """The row's own agent ran the tell: its pane holds that rollout, so it clears, and the other
    session's rollout in the folder neither hides it nor adds to it."""
    _unsent(spawned)
    spawned.rollout([OTHERS_TASK], began=_soon(), held=False)
    assert spawned.listed().state == "attention"

    spawned.rollout(["a fleet tell of other words"], began=_soon())

    assert spawned.listed().state != "attention"
    assert spawned.row().first_prompt == "acked"
    with store_session() as store:
        assert store.get_meta(f"{fleet_service.FIRST_PROMPT_META}{spawned.agent.id}") is None


def test_the_same_message_another_agent_ran_is_not_this_rows(spawned: Spawned) -> None:
    """Two agents given the same message in one folder used to settle each other's: the one
    that ran it took the other's, which never did."""
    spawned.rollout([MESSAGE], held=False)
    spawned.later(46)

    status = spawned.listed()

    assert status.state == "attention"
    assert spawned.row().first_prompt == "unsent"


# --- what asks the process table


@pytest.mark.parametrize(
    "case", ["no-rollout", "other-folder", "own-context-only"], ids=lambda case: case
)
def test_the_process_table_is_asked_only_when_a_rollout_gave_a_prompt_to_attribute(
    spawned: Spawned, processes: Processes, tmp_path: Path, case: str
) -> None:
    """``ps`` and ``lsof`` cost 0.1 to 0.2 s on a loaded machine and a listing comes every second
    or two for as long as a row is unsent: a folder where nothing ran costs none of it."""
    if case == "other-folder":
        spawned.rollout(["theirs"], cwd=tmp_path / "elsewhere")
    elif case == "own-context-only":
        spawned.rollout([])
    spawned.later(46)

    for _ in range(3):
        assert spawned.listed().state == "attention"

    assert processes.reads == 0


def test_the_record_says_whose_it_is_when_the_table_is_read_and_when_it_is_not(
    spawned: Spawned, processes: Processes
) -> None:
    mine = spawned.rollout(["mine"])
    spawned.rollout(["theirs"], held=False)
    since = datetime.now(tz=UTC) - timedelta(minutes=1)
    pane = PaneProcesses(lambda: PANE_PID)

    record = codex_prompts.prompts_since(spawned.home, spawned.cwd, since, pane)
    assert (record.prompts, record.own) == (("mine",), True), mine

    processes.readable = False
    record = codex_prompts.prompts_since(
        spawned.home, spawned.cwd, since, PaneProcesses(lambda: PANE_PID)
    )
    assert sorted(record.prompts) == ["mine", "theirs"] and record.own is False

    record = codex_prompts.prompts_since(
        spawned.home, spawned.cwd, since, PaneProcesses(lambda: None)
    )
    assert record.own is False, "a pane whose pid tmux will not give is not told either"


# --- where the process table cannot be read


def test_with_the_table_unreadable_a_row_alone_in_its_folder_is_still_taken_by_a_later_prompt(
    spawned: Spawned, processes: Processes
) -> None:
    """No attribution to be had: the row is the only one in its folder, so a prompt there is the
    row's (what #17 did, and what the docs say a session outside the fleet can still defeat)."""
    processes.readable = False
    _unsent(spawned)
    spawned.rollout(["a fleet tell of other words"], began=_soon(), held=False)

    assert spawned.listed().state != "attention"
    assert spawned.row().first_prompt == "acked"


def test_with_the_table_unreadable_another_live_row_in_the_folder_leaves_an_unsent_row_unsent(
    spawned: Spawned,
    tmux: FakeTmux,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    processes: Processes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With two rows in the folder and no way to tell whose prompt it was, only the message's own
    words take either: the manager keeps being told, which is the safe way to be wrong."""
    processes.readable = False
    _second(spawned, tmux, project, processes, monkeypatch)
    _unsent(spawned)
    spawned.rollout([OTHERS_TASK], began=_soon(), held=False)

    for _ in range(2):
        assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"

    spawned.rollout([MESSAGE], began=_soon(), held=False)  # its own words: taken, as before

    assert spawned.listed().state != "attention"
    assert spawned.row().first_prompt == "acked"


def test_with_the_table_unreadable_an_ended_row_in_the_folder_is_no_other(
    spawned: Spawned,
    tmux: FakeTmux,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    processes: Processes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes.readable = False
    stopped = _second(spawned, tmux, project, processes, monkeypatch)
    with store_session() as store:
        store.end_fleet_agent(stopped.agent.id, ended_by="stopped")
        ended = store.get_fleet_agent(stopped.agent.id)
        assert ended is not None and ended.ended_at is not None
    _unsent(spawned)
    spawned.rollout(["a fleet tell of other words"], began=_soon(), held=False)

    assert spawned.listed().state != "attention", "an ended row is not running there"


def test_with_the_table_unreadable_a_store_that_will_not_say_leaves_the_row_unsent(
    spawned: Spawned, processes: Processes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether the row is alone is a question put to the store, and a store that fails to answer
    says no: the manager keeps being told."""
    processes.readable = False
    _unsent(spawned)
    spawned.rollout(["a fleet tell of other words"], began=_soon(), held=False)

    def refuse(self: Any) -> Any:
        raise RuntimeError("database is locked")

    row = spawned.row()
    with monkeypatch.context() as refusing:
        refusing.setattr(SqliteStore, "live_fleet_agents", refuse)
        assert fleet_service._alone_in_its_folder(row) is False
    assert fleet_service._alone_in_its_folder(row) is True


def test_a_live_row_of_another_folder_does_not_count_either(
    spawned: Spawned, processes: Processes, tmp_path: Path
) -> None:
    processes.readable = False
    other = tmp_path / "another-checkout"
    other.mkdir()
    with store_session() as store:
        row = store.get_fleet_agent(spawned.agent.id)
        assert row is not None
        store.upsert_fleet_agent(
            row.model_copy(update={"id": "agt_other", "label": "coder-9", "cwd": other})
        )
    _unsent(spawned)
    spawned.rollout(["a fleet tell of other words"], began=_soon(), held=False)

    assert spawned.listed().state != "attention"


def test_a_pane_tmux_will_not_give_a_pid_for_is_read_like_a_table_that_cannot_be(
    spawned: Spawned,
    tmux: FakeTmux,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    processes: Processes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux.pids.pop(spawned.agent.pane_id)
    _second(spawned, tmux, project, processes, monkeypatch)
    _unsent(spawned)
    spawned.rollout([OTHERS_TASK], began=_soon(), held=False)

    assert spawned.listed().state == "attention", "two rows, no pid: only the message's words"


# --- what leaves an unsent row unsent


def _prompt_line(text: str) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
    )


def _truncated(spawned: Spawned, tmp_path: Path) -> None:
    path = spawned.rollout(["something the human typed instead"], began=_soon())
    with path.open("a", encoding="utf-8") as handle:  # a prompt Codex is still writing
        handle.write(_prompt_line("half a prompt")[:-12])


def _same_prompt_otherwise_spaced(spawned: Spawned, tmp_path: Path) -> None:
    spawned.rollout(["  something  the human\ntyped instead \n"], began=_soon())


def _blank_prompt(spawned: Spawned, tmp_path: Path) -> None:
    spawned.rollout(["   \n  "], began=_soon())


def _no_session_meta(spawned: Spawned, tmp_path: Path) -> None:
    path = spawned.rollout(["a prompt of a rollout that names no folder"], began=_soon())
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")


def _another_folder(spawned: Spawned, tmp_path: Path) -> None:
    spawned.rollout(["a prompt in another folder"], cwd=tmp_path / "elsewhere", began=_soon())


def _before_the_spawn(spawned: Spawned, tmp_path: Path) -> None:
    spawned.rollout(["a prompt long before"], began=spawned.given_at() - timedelta(minutes=5))


def _codexs_own_context(spawned: Spawned, tmp_path: Path) -> None:
    spawned.rollout([], began=_soon())


def _a_big_rollout_growing_with_no_prompts(spawned: Spawned, tmp_path: Path) -> None:
    path = spawned.rollout([], began=_soon())
    filler = json.dumps({"type": "event_msg", "payload": {"pad": "x" * 4_000}}) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(filler * 900)  # 3.6 MB: past the head and the tail read
    spawned.listed()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(filler * 10)  # and it grows


@pytest.mark.parametrize(
    "arrange",
    [
        _truncated,
        _same_prompt_otherwise_spaced,
        _blank_prompt,
        _no_session_meta,
        _another_folder,
        _before_the_spawn,
        _codexs_own_context,
        _a_big_rollout_growing_with_no_prompts,
    ],
    ids=lambda arrange: arrange.__name__.strip("_"),
)
def test_none_of_these_is_a_prompt_the_row_ran_after_it_read_unsent(
    spawned: Spawned, tmp_path: Path, arrange: Any
) -> None:
    """The tester's probes of the rule, each held by the row's own pane so that the rule's own
    reading, and not whose it is, leaves the row where it was."""
    _unsent(spawned, "something the human typed instead")

    arrange(spawned, tmp_path)

    for _ in range(2):
        assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"
