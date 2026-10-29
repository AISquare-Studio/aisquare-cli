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
  still take the row and no other prompt does, not even when it is the only live row in its
  folder: "alone" is a fact about the moment of the listing, and the prompt may be that of an
  agent that ran its task in the folder and ended since (tester-24b); the row's detail says
  so.

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
from aisquare.core.store import store_session
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


def _named(spawned: Spawned) -> ProjectInfo:
    """The project as a listing reads it (its codename), for the detail it writes."""
    with store_session() as store:
        return store.get_project(spawned.project.id) or spawned.project


def test_with_the_table_unreadable_a_row_alone_in_its_folder_is_not_taken_by_a_later_prompt(
    spawned: Spawned, processes: Processes
) -> None:
    """No attribution to be had, and the row is the only one in its folder: still no. "Alone" is
    asked at the listing, about the moment, and says nothing of whose the prompt was (the next
    test is the way that goes wrong); a session outside the fleet there could run it too."""
    processes.readable = False
    _unsent(spawned)
    spawned.rollout(["a fleet tell of other words"], began=_soon(), held=False)

    for _ in range(3):
        assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"


def test_with_the_table_unreadable_a_row_that_ran_a_task_and_ended_does_not_take_the_unsent_row(
    spawned: Spawned,
    tmux: FakeTmux,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    processes: Processes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tester-24b's repro 1 (unit): A and B in one folder, A's message lost, B runs its own task.
    While B lives A stays unsent; B is stopped, so A is "alone", and B's old prompt, which is in
    the record and not among those A saw when it read unsent, must not take A: its agent ran
    nothing, idles at its prompt, and its manager is still waiting."""
    processes.readable = False
    other = _second(spawned, tmux, project, processes, monkeypatch)
    _unsent(spawned)
    other.rollout([OTHERS_TASK], began=_soon(), held=False)
    for _ in range(3):
        assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"

    with store_session() as store:
        store.end_fleet_agent(other.agent.id, ended_by="stopped")
        ended = store.get_fleet_agent(other.agent.id)
        assert ended is not None and ended.ended_at is not None

    for _ in range(3):
        status = spawned.listed()
        assert status.state == "attention", "B's old prompt took a row whose agent ran nothing"
    assert spawned.row().first_prompt == "unsent"
    with store_session() as store:
        assert store.get_meta(f"{fleet_service.FIRST_PROMPT_META}{spawned.agent.id}") is not None


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


def test_with_the_table_unreadable_the_message_itself_takes_a_row_alone_in_its_folder(
    spawned: Spawned, processes: Processes
) -> None:
    """What the row asked for ran, whoever's pane held it: its digest is the one rule that needs
    no attribution, and the one #17 had."""
    processes.readable = False
    _unsent(spawned)
    spawned.rollout(["something else a human typed"], began=_soon(), held=False)
    assert spawned.listed().state == "attention"

    spawned.rollout(["  " + MESSAGE.replace(" ", "  ") + "\n"], began=_soon(), held=False)

    assert spawned.listed().state != "attention"
    assert spawned.row().first_prompt == "acked"


def test_an_unsent_row_says_where_only_the_message_itself_clears_it(
    spawned: Spawned, processes: Processes
) -> None:
    """The detail is the same two ways on, and, where the record could not be told from other
    sessions' (a prompt there, the table unreadable), the reason a ``tell`` of other words does
    not clear it: the manager is not left to find that out. Where it can be read, no such word."""
    named = _named(spawned)
    plain = fleet_service.unsent_detail(named, spawned.agent.label)
    blind = fleet_service.unsent_detail(named, spawned.agent.label, blind=True)
    assert blind == plain + fleet_service.BLIND_DETAIL
    assert "only the message itself clears this" in blind and "no lsof" in blind
    assert fleet_service.BLIND_DETAIL not in plain

    _unsent(spawned)
    assert spawned.listed().detail == plain, "no prompt of the folder to attribute: nothing blind"

    spawned.rollout([OTHERS_TASK], began=_soon(), held=False)
    assert spawned.listed().detail == plain, "the table read: another agent's, and said so plain"

    processes.readable = False
    status = spawned.listed()
    assert status.state == "attention" and status.detail == blind


def test_a_tell_of_other_words_with_the_table_unreadable_leaves_the_detail_saying_why(
    spawned: Spawned, processes: Processes
) -> None:
    """The case the detail is for: the row's own agent runs the human's ``fleet tell``, nothing
    can tell it from another session's, and the row stays unsent. The same message told again
    is the way out, and takes it."""
    processes.readable = False
    _unsent(spawned)
    named = _named(spawned)
    spawned.rollout(["do something else instead"], began=_soon(), held=False)

    status = spawned.listed()
    assert status.state == "attention"
    assert status.detail == fleet_service.unsent_detail(named, spawned.agent.label, blind=True)

    spawned.rollout([MESSAGE], began=_soon(), held=False)
    assert spawned.listed().state != "attention"
    assert spawned.row().first_prompt == "acked"


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
