"""Failing open is half the doctrine; saying what it cost is the other half.

The four hook boundaries end in::

    except Exception:  # never disrupt the agent
        return

The comment is right about what it protects and silent about what it costs.
Measured at 71cc37d in a throwaway home with a realistic payload on stdin:

* healthy store → ``hook session-start`` exits 0 and emits **778 bytes** of
  context on stdout;
* damaged store → exits 0 and emits **zero bytes**, on stdout *and stderr*.

The 778 bytes are the control: the healthy path really does inject, so the
damaged case is a genuine loss and not a probe that measured nothing.

WHY THIS IS THE SURFACE THAT MATTERS. ``session-start`` is how a session gets
its aisquare context and ``user-prompt-submit`` is how teammate deltas reach a
running session — the team-bus itself. A damaged store silently stops both, for
every session, every prompt. The agent does not know it is running blind and
neither does the operator; ``doctor`` would say so, but nothing tells them to
run it. Compare ``launch``, where the same doctrine is applied correctly:
"board: context.db unreadable — launching without a board row".

STDOUT IS NOT A LOG HERE. For ``session-start`` and ``user-prompt-submit``,
stdout BECOMES THE AGENT'S CONTEXT — a diagnostic printed there is injected
into the model's prompt. So every test below pins stdout as well as stderr, and
a fix that "works" by printing the reason to stdout fails these outright. That
would be a worse defect than the silence it replaced.

WHY EVERY OCCURRENCE AND NOT ONCE PER SESSION. Warning once would need
somewhere to record that it already warned — and the thing that is broken IS
the place we would record it. Any dedupe state for a damaged store either lives
in the damaged store or adds a write to a home we already know is unhealthy.
Each occurrence is also a real loss: that prompt genuinely did not get its
delta. The volume is bounded by fixing the store, which is what the line says
to do.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths

#: All FIVE boundaries, not the four I first measured — `notification` has
#: the identical swallow and would have been the one left silent.
_HOOKS = ["session-start", "user-prompt-submit", "stop", "session-end", "notification"]

_CORRUPT = b"this is not a database, and the agent deserves to be told"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Same shape as tests/test_hooks.py: a real cwd for the payload to name."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _payload(work_dir: Path) -> str:
    return json.dumps(
        {
            "session_id": "probe123",
            "cwd": str(work_dir),
            "prompt": "hello",
            "transcript_path": "/tmp/probe.jsonl",
        }
    )


def _damage_the_store() -> Path:
    """Corrupt a store that really exists — the home is created on demand."""
    paths.ensure_home()
    db = paths.db_path()
    db.write_bytes(_CORRUPT)
    return db


def _wedge_the_store(runner: CliRunner) -> None:
    """A store left mid-migration: §0b's race, reproduced by rewinding.

    Needs a real migrated store first, so a command creates one.
    """
    runner.invoke(app, ["context", "list"], catch_exceptions=False)
    connection = sqlite3.connect(str(paths.db_path()))
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert version > 0, "fixture premise: a migrated store to rewind"
    connection.execute(f"PRAGMA user_version = {version - 1}")
    connection.commit()
    connection.close()


@pytest.mark.parametrize("hook", _HOOKS)
def test_a_damaged_store_never_costs_the_exit_code(
    runner: CliRunner, work_dir: Path, hook: str
) -> None:
    """The clause that must not regress while adding the other one.

    "Never disrupt the agent" is the doctrine's first half and it is already
    correct. A fix that made the hooks louder by making them fail would trade
    one defect for a worse one.
    """
    _damage_the_store()

    result = runner.invoke(app, ["hook", hook], input=_payload(work_dir))

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("hook", _HOOKS)
def test_a_damaged_store_says_so_on_stderr(runner: CliRunner, work_dir: Path, hook: str) -> None:
    """The half that was missing: the reason, on the channel that is not context."""
    db = _damage_the_store()

    result = runner.invoke(app, ["hook", hook], input=_payload(work_dir))

    assert result.stderr.strip(), f"{hook} failed open in total silence"
    assert str(db) in result.stderr, "name the file, or it is not actionable"
    assert "doctor" in result.stderr, "point at the command that explains it"


@pytest.mark.parametrize("hook", _HOOKS)
def test_the_reason_never_reaches_stdout(runner: CliRunner, work_dir: Path, hook: str) -> None:
    """stdout is injected into the agent's prompt, so it stays byte-identical.

    This is the assertion that makes the fix safe rather than merely present:
    the obvious implementation — ``typer.echo(reason)`` without ``err=True`` —
    passes the stderr test above and silently starts feeding diagnostics to
    the model on every prompt.
    """
    _damage_the_store()

    result = runner.invoke(app, ["hook", hook], input=_payload(work_dir))

    assert result.stdout == "", f"{hook} leaked a diagnostic into injected context"


def test_one_line_not_a_traceback(runner: CliRunner, work_dir: Path) -> None:
    """A hook's stderr is read by a human skimming a transcript, not a debugger."""
    _damage_the_store()

    result = runner.invoke(app, ["hook", "session-start"], input=_payload(work_dir))

    assert "Traceback" not in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1, result.stderr


def test_the_healthy_path_is_untouched(runner: CliRunner, work_dir: Path) -> None:
    """The control the whole finding rests on, asserted rather than assumed.

    A "fix" that broke context injection would still satisfy every assertion
    above, because they all describe the damaged case.
    """
    runner.invoke(app, ["context", "add", "prefer tabs", "--user"])

    result = runner.invoke(app, ["hook", "session-start"], input=_payload(work_dir))

    assert result.exit_code == 0
    assert "prefer tabs" in result.stdout, "the healthy path stopped injecting"
    assert result.stderr.strip() == "", "a healthy store must stay silent"


def test_an_unrelated_failure_also_says_why(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The swallow catches everything, so everything it swallows gets a reason.

    Narrowing the reason to store errors would leave every other bug in the
    hook path exactly as invisible as this one was — and a hook that silently
    stops working is precisely the failure being fixed.
    """
    from aisquare.services import hooks as hooks_service

    def _boom(*_a: object, **_kw: object) -> None:
        raise ValueError("something else entirely")

    monkeypatch.setattr(hooks_service, "session_start_context", _boom)

    result = runner.invoke(app, ["hook", "session-start"], input=_payload(work_dir))

    assert result.exit_code == 0
    assert result.stdout == ""
    assert "something else entirely" in result.stderr


def test_a_malformed_payload_is_not_an_error_worth_announcing(
    runner: CliRunner,
) -> None:
    """Not every early return is a failure.

    ``hook`` is invoked with junk on stdin by anything that mis-wires it, and
    the existing contract is to shrug. Announcing that as a cost would make
    the new line fire on a healthy machine, which is how a warning gets muted.
    """
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input="not json at all")

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr.strip() == ""


def test_the_store_error_is_named_not_just_reported(runner: CliRunner, work_dir: Path) -> None:
    """ "Something went wrong" is silence with extra steps."""
    _damage_the_store()

    result = runner.invoke(app, ["hook", "session-start"], input=_payload(work_dir))

    assert "file is not a database" in result.stderr


@pytest.mark.parametrize("hook", _HOOKS)
def test_a_wedged_store_is_announced_too(runner: CliRunner, work_dir: Path, hook: str) -> None:
    """The other damaged shape, and the one §0b of the cutover runbook exists for.

    Keyed on the swallow rather than on a particular sqlite error, so this
    holds for whatever the store does next.
    """
    _wedge_the_store(runner)

    result = runner.invoke(app, ["hook", hook], input=_payload(work_dir))

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr.strip(), f"{hook} lost its context in silence"


@pytest.mark.parametrize("hook", _HOOKS)
def test_the_line_says_what_THIS_hook_cost(runner: CliRunner, work_dir: Path, hook: str) -> None:
    """Three of the five do not inject context, so one shared sentence was wrong.

    A warning that misdescribes what happened is worse than a generic one: it
    is a false statement printed on every turn, and the reader who checks it
    stops trusting the rest of the line.
    """
    from aisquare.cli.hook import _COST

    _damage_the_store()

    result = runner.invoke(app, ["hook", hook], input=_payload(work_dir))

    assert _COST[hook] in result.stderr, f"{hook} described someone else's cost"
