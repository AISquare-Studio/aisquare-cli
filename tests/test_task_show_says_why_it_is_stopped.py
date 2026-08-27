"""``task show`` says it shows "one task in full" and omits why it is stopped.

The reason is NOT dropped. `task block <id> --reason "…"` persists it as a
``task_blocked`` team event whose text is ``"<title> — <reason>"``, and
`task show` never reads it back. @9bbc8ed7 found that by searching all thirteen
tables of the store for the string rather than by reading the schema, which is
why this is a READ-path fix with no migration and no new column: the state
already exists and duplicating it would give the board two answers.

WHY IT MATTERS: the two tasks gating the north star are `blocked`, and the first
thing anyone does with a blocked task is ask why. The recovery — reading the
event stream — works today, so this is a legibility defect rather than a
lock-out, and it is fixed here as one shared render because ALL THREE finishers
write the same shape:

    task block  --reason   -> task_blocked
    task reopen --reason   -> task_reopened
    task done   --note     -> task_done

(The contract called those `--feedback` and `--outcome`; measured against the
running CLI they are `--reason` and `--note`. The gap is shared regardless.)

THE STALE-NOTE HAZARD, and why the lookup is keyed on the CURRENT status: a task
that was blocked and then claimed must not still show the blocked reason. Asking
"what is this task's status, and what is the newest event of the kind that
produces that status" answers that by construction rather than by remembering to
clear something.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _add(runner: CliRunner, title: str) -> str:
    out = runner.invoke(app, ["--json", "task", "add", title])
    assert out.exit_code == 0, out.output
    return str(json.loads(out.stdout)["id"])


def _show(runner: CliRunner, ref: str) -> tuple[str, dict[str, object]]:
    human = runner.invoke(app, ["task", "show", ref])
    machine = runner.invoke(app, ["--json", "task", "show", ref])
    assert human.exit_code == 0, human.output
    assert machine.exit_code == 0, machine.output
    return human.stdout, json.loads(machine.stdout)


def test_a_blocked_task_says_why(runner: CliRunner) -> None:
    """The case the two north-star blockers are in."""
    ref = _add(runner, "E2E on staging")
    blocked = runner.invoke(app, ["task", "block", ref, "--reason", "needs a read credential"])
    assert blocked.exit_code == 0, blocked.output

    human, payload = _show(runner, ref)

    assert "needs a read credential" in human, human
    assert payload["stopped_because"] == "needs a read credential", payload
    # The title is the event's prefix, not part of the reason.
    assert payload["stopped_because"] is not None
    assert "E2E on staging" not in str(payload["stopped_because"])


def test_the_newest_reason_wins_when_a_task_is_blocked_twice(runner: CliRunner) -> None:
    """The stream holds both, so which one shows has to be a decision.

    Left unpinned this is incidental — whichever the query happens to return —
    and an operator reading a superseded reason is worse off than one reading
    none, because a stale answer does not look stale.
    """
    ref = _add(runner, "two walls")
    runner.invoke(app, ["task", "block", ref, "--reason", "first wall"])
    runner.invoke(app, ["task", "claim", ref])
    runner.invoke(app, ["task", "block", ref, "--reason", "second wall"])

    human, payload = _show(runner, ref)

    assert payload["stopped_because"] == "second wall", payload
    assert "first wall" not in human, human


def test_a_finish_with_no_note_renders_nothing(runner: CliRunner) -> None:
    """The control: absent must read as absent, not as an empty string.

    With no note the event text IS the title, so a renderer that strips a prefix
    without checking would emit "" — a blank line under a bold label, which
    reads as "the note is empty" rather than "none was given".

    THIS USES `done`, NOT `block`, AND THE FIRST VERSION USED `block` AND PROVED
    NOTHING. `task block --reason` is REQUIRED, so `task block <id>` exits 2 on
    usage and the task stays `todo` — the assertion then passed through the
    "no events" branch and never reached the one it was written for. My own
    sabotage of the prefix branch left all eight green, which is how it was
    found. `done` takes an optional `--note`, so the no-note state is reachable
    there and only there; the contract's "blocked with no reason" is a state the
    CLI cannot produce.
    """
    ref = _add(runner, "silent finish")
    finished = runner.invoke(app, ["task", "done", ref])
    assert finished.exit_code == 0, finished.output

    human, payload = _show(runner, ref)

    # The premise: this task IS in a status the lookup maps, so a None here is
    # the prefix branch answering and not the lookup declining to look.
    assert payload["status"] == "done", payload
    assert payload["stopped_because"] is None, payload
    assert "stopped because" not in human.lower(), human


def test_an_open_task_renders_no_reason_at_all(runner: CliRunner) -> None:
    ref = _add(runner, "nothing wrong here")

    human, payload = _show(runner, ref)

    assert payload["stopped_because"] is None, payload
    assert "stopped because" not in human.lower(), human


def test_claiming_a_blocked_task_drops_its_reason(runner: CliRunner) -> None:
    """The stale-note control, and the reason the lookup is keyed on status.

    The event is still in the stream — nothing deletes it — so a renderer that
    asks "is there a task_blocked for this task" keeps showing it forever. This
    asks "what produced the CURRENT status", which cannot go stale.
    """
    ref = _add(runner, "picked back up")
    runner.invoke(app, ["task", "block", ref, "--reason", "was stuck yesterday"])
    runner.invoke(app, ["task", "claim", ref])

    human, payload = _show(runner, ref)

    assert payload["status"] == "doing", payload
    assert payload["stopped_because"] is None, payload
    assert "was stuck yesterday" not in human, human


@pytest.mark.parametrize(
    ("verb", "flag", "note"),
    [("done", "--note", "shipped it"), ("reopen", "--reason", "tests were red")],
    ids=["done-note", "reopen-reason"],
)
def test_the_other_two_finishers_share_the_render(
    runner: CliRunner, verb: str, flag: str, note: str
) -> None:
    """Contract step 4: check the siblings BEFORE fixing block, then fix once.

    All three write ``"<title> — <note>"`` through the same shape, so all three
    were equally unreadable and one render answers for them.
    """
    ref = _add(runner, "shared shape")
    if verb == "reopen":
        runner.invoke(app, ["task", "claim", ref])
    result = runner.invoke(app, ["task", verb, ref, flag, note])
    assert result.exit_code == 0, result.output

    human, payload = _show(runner, ref)

    assert payload["stopped_because"] == note, payload
    assert note in human, human


def test_a_missing_event_never_costs_an_exit_code(runner: CliRunner) -> None:
    """Fail-open, per the contract: `task show` reads, it does not depend.

    Simulated by asking for a task whose status has no matching event at all —
    a task marked done through a path that emitted nothing would land here, and
    the answer must be "no note", never a traceback or a non-zero exit.
    """
    ref = _add(runner, "no event for this status")
    from aisquare.services import team as team_service

    task = team_service.show_task(ref)
    assert team_service.stopped_because(task) is None
