"""A damaged store must not turn a command into a stack trace.

``context.db`` can wedge — that is why the cutover runbook has a §0b at all,
and why it has a wedge-recovery step. What nobody had measured is what the
CLI DOES when it is wedged. Measured at 5200357 against a corrupt store, FOURTEEN commands let a
``DatabaseError: file is not a database`` escape, which reaches the operator as
a Rich-rendered Python traceback with source frames:

    status  log  inject  init
    context list  context export  context preview
    ctx list  ctx export  ctx preview
    project list  project info  workspace list  workspace info

A hand-run shell sweep of "read-only sounding" leaf names found nine of these.
The count is fourteen because the sweep filtered on names it guessed were
read-only — `export` and `preview` did not sound like reads. Enumerating the
tree and invoking everything is the same instrument with the guessing removed,
which is the whole argument for a detector over another sweep.

``status`` is the one that decides this. Runbook §0b is
``aisquare status >/dev/null`` — the first thing after preflight, run
*specifically* to create and migrate the store. If the store is wedged at
08:05, the command the runbook uses to warm it prints a stack trace before the
operator has reached §1, and they will reasonably conclude the CLI is broken.

WHY A DETECTOR RATHER THAN NINE MORE FIXES. Six of these boundaries were made
legible one at a time earlier in this shift. @9bbc8ed7 then found a seventh by
hand. This file found nine more by machine in one pass. A class that is being
retired one instance at a time, by whoever happens to trip over it, needs the
thing that counts it — otherwise instance sixteen arrives the same way, and the
next person to find it is an operator.

WHAT THIS ASSERTS, PRECISELY: that no command raises an UNHANDLED exception.
That is the condition which produces the traceback — Typer renders one for
anything that is not a ``SystemExit``. A command may still fail, exit non-zero,
and say why; ``click``'s own usage errors (exit 2) are ``SystemExit`` and pass.
This guard is about legibility, not about success.

IT IS DELIBERATELY NOT AN ASSERTION ABOUT THE MESSAGE. What each command should
SAY when the store is unreadable is a per-command judgement, and
tsk_01m08ygythttzm2y9hc53qehkt is deciding it for `init` and `doctor` right now.
A guard that also demanded particular wording would collide with that work and
would have to be relaxed for every command that words it differently.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths

#: Not invoked, each for a reason that is about the HARNESS and never about the
#: command being expected to fail. Every name is checked to still exist below,
#: so a rename cannot quietly widen this list into a hiding place.
UNINVOKED = {
    "serve": "binds a port and blocks",
    "launch": "spawns a real agent process",
    "team spawn": "spawns a real agent process",
    "login": "waits on interactive input",
    "logout": "clears credentials on the developer's own machine",
    "open": "launches a browser",
    "uninstall": "removes the installation running the test",
    "upgrade": "reaches the network to reinstall",
    "sync": "reaches the network",
    "project onboard": "packs a codebase snapshot; minutes, not seconds",
    "workspace onboard": "packs a codebase snapshot; minutes, not seconds",
    "team distill": "calls a model",
}

CORRUPT = b"this is not a sqlite database, and open_store must say so in words"

#: The commands that raise TODAY, measured. This is a RATCHET, not an allow
#: list, and the difference is the whole reason it is acceptable to introduce a
#: detector over a class that is already broken:
#:
#:   * a command NOT listed here that starts raising fails the guard — that is
#:     instance sixteen, caught by machine instead of by an operator;
#:   * a command listed here that STOPS raising also fails the guard, naming
#:     itself, so the list cannot outlive the defect it describes.
#:
#: Every one of these dies in the same frame — ``core.store.open_store`` at
#: ``PRAGMA journal_mode = WAL``. One legible boundary there empties this list
#: in a single change, which is the measurement handed to
#: tsk_01m08ygythttzm2y9hc53qehkt rather than a fix taken out of its hands:
#: `init` and `doctor`'s remediation are that task's to word, and wording them
#: from here would be deciding someone else's claim for them.
STILL_RAISES = {
    "context export",
    "context list",
    "context preview",
    "ctx export",
    "ctx list",
    "ctx preview",
    "init",
    "inject",
    "log",
    "project info",
    "project list",
    "status",
    "workspace info",
    "workspace list",
}


def _leaves() -> list[list[str]]:
    """Every runnable command in the tree, deepest names included."""
    found: list[list[str]] = []

    def walk(node: object, chain: list[str]) -> None:
        children = getattr(node, "commands", {}) or {}
        if not children:
            found.append(chain)
            return
        for name, child in sorted(children.items()):
            walk(child, [*chain, name])

    walk(get_command(app), [])
    return found


def _invoked() -> list[list[str]]:
    return [chain for chain in _leaves() if " ".join(chain) not in UNINVOKED]


@pytest.fixture
def damaged_store(isolated_home: Path) -> Path:
    """A machine that has been used, whose database is now unreadable.

    Built by letting the CLI create the store and then overwriting it, rather
    than by writing a file called context.db into an empty directory: the second
    is a machine that was never initialised, which is a different case with a
    different correct answer.
    """
    runner = CliRunner()
    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)
    database = paths.db_path()
    assert database.exists(), "the fixture did not produce a store to damage"
    database.write_bytes(CORRUPT)
    return database


def test_the_uninvoked_list_names_only_real_commands() -> None:
    """A stale exclusion is a hiding place.

    If a command is renamed, its entry here stops matching and the entry becomes
    an unexamined claim that some command is unsafe to run. Worse, nothing would
    say so — the guard would simply cover one command more and no one would know
    which.
    """
    known = {" ".join(chain) for chain in _leaves()}

    unknown = sorted(name for name in UNINVOKED if name not in known)

    assert not unknown, f"these are excluded but no longer exist: {unknown}"


def test_the_guard_actually_covers_the_tree() -> None:
    """Guard the guard: an empty walk would satisfy every assertion below."""
    invoked = _invoked()

    assert len(invoked) >= 90, f"only {len(invoked)} commands would be invoked"
    names = {" ".join(chain) for chain in invoked}
    for required in ("status", "log", "inject", "context list", "project list"):
        assert required in names, f"{required} is not being invoked"


@pytest.mark.parametrize("chain", _invoked(), ids=lambda chain: " ".join(chain))
def test_a_damaged_store_never_produces_a_traceback(chain: list[str], damaged_store: Path) -> None:
    """The property, one test per command so a failure names the command.

    Parametrised rather than looped: a loop reports the first failure and hides
    the rest, and the useful output here is WHICH commands are affected — that
    list is what tells whoever fixes it whether the right boundary is one
    command or `open_store`.
    """
    name = " ".join(chain)
    raised = _raises_unhandled(chain)

    if name in STILL_RAISES:
        assert raised is not None, (
            f"`aisquare {name}` no longer raises — good. Remove it from "
            "STILL_RAISES so the list keeps describing the truth; a ratchet that "
            "is not tightened is an allow list."
        )
        return

    assert raised is None, (
        f"`aisquare {name}` raised {type(raised).__name__}: {raised}\n"
        "An unhandled exception reaches the operator as a Python traceback. A "
        "store that cannot be opened is a condition to report in one line, "
        "naming the file and what to do about it — not a crash."
    )


def _raises_unhandled(chain: list[str]) -> BaseException | None:
    """The exception a command lets escape, or None.

    `SystemExit` is not one: click raises it for usage errors and for its own
    clean non-zero exits, and both are already legible.
    """
    result = CliRunner().invoke(app, chain, catch_exceptions=True)
    if result.exception is None or isinstance(result.exception, SystemExit):
        return None
    return result.exception


def test_the_ratchet_names_only_commands_that_exist() -> None:
    """Same hazard as a stale exclusion, on the other list.

    A renamed command would leave its old name here forever, silently claiming
    a defect that no longer has a home, while the renamed command itself walked
    out of coverage.
    """
    known = {" ".join(chain) for chain in _leaves()}

    unknown = sorted(name for name in STILL_RAISES if name not in known)

    assert not unknown, f"STILL_RAISES names commands that do not exist: {unknown}"


@pytest.mark.usefixtures("damaged_store")
def test_they_all_fail_in_the_same_place() -> None:
    """The measurement that tells whoever fixes this where the boundary belongs.

    Fourteen commands, one frame. If this ever stops holding, the class has
    become several classes and a single legible boundary would no longer close
    it — which is exactly the thing a fixer needs to know BEFORE choosing where
    to put the fix, not after.
    """
    frames: set[str] = set()
    for chain in _invoked():
        if " ".join(chain) not in STILL_RAISES:
            continue
        raised = _raises_unhandled(chain)
        if raised is None:
            continue
        traceback = raised.__traceback__
        while traceback is not None and traceback.tb_next is not None:
            traceback = traceback.tb_next
        if traceback is not None:
            frames.add(traceback.tb_frame.f_code.co_name)

    assert frames == {"open_store"}, (
        f"the raising commands no longer share one boundary: {sorted(frames)}. "
        "A single legible error at open_store no longer closes the class."
    )
