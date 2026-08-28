"""A damaged store must not turn a command into a stack trace.

``context.db`` can wedge — that is why the cutover runbook has a §0b at all,
and why it has a wedge-recovery step. What nobody had measured is what the
CLI DOES when it is wedged. Measured at 5200357 against a corrupt store,
FOURTEEN commands let a ``DatabaseError: file is not a database`` escape, which
reached the operator as a Rich-rendered Python traceback with source frames:

    status  log  inject  init
    context list  context export  context preview
    ctx list  ctx export  ctx preview
    project list  project info  workspace list  workspace info

They are all legible now — ``open_store`` raises ``StoreUnopenable`` and the
root group translates it in one place, which is what emptied ``STILL_RAISES``
below. The list above is the measurement that chose that seam, kept because it
is what makes an empty ratchet mean something.

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

import sqlite3
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
    "fleet attach": "replaces the process with `tmux attach` (os.execvp)",
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
#: Every one of them died in the same frame — ``core.store.open_store`` at
#: ``PRAGMA journal_mode = WAL`` — and that is exactly how it was fixed: one
#: boundary, not fourteen. The prediction in this comment held.
#: EMPTIED by tsk_01m08ygythttzm2y9hc53qehkt, in the single change this comment
#: predicted: ``open_store`` now raises ``StoreUnopenable`` and the root group
#: translates it once, so all fourteen report in one line instead of raising.
#: Kept rather than deleted — an empty ratchet is the strongest form of this
#: guard: every command in the tree is now held to the property, and the next
#: instance has nowhere to be listed.
STILL_RAISES: set[str] = set()

#: The same ratchet for QUERY-TIME damage, which the seam does not yet reach.
#: Measured at 984a3b9: these nine still raise when a SELECT hits a zeroed page,
#: while the at-open list above is empty. Both directions fail the guard, so
#: this cannot rot into an allow list and cannot outlive the defect.
#:
#: Deliberately not fixed here. Widening the seam means catching
#: sqlite3.DatabaseError at a point where OperationalError — which IS a
#: DatabaseError — must keep meaning "somebody else holds the lock" rather than
#: "your board is damaged". @9bbc8ed7 built `is_locked_error` for exactly that
#: distinction and the widening is theirs to design; this only makes sure the
#: gap cannot pass for closed in the meantime.
#: EMPTIED by tsk_01m096hzpy9eff2rsnj6349y2y, the widening this comment handed
#: over. The root group now also translates a corrupt-file error found by a
#: QUERY, keyed on ``is_corrupt_error`` so that "database is locked" keeps
#: meaning contention and "no such table" keeps its traceback. Both ratchets are
#: empty now; each still fails in both directions, so neither can rot.
STILL_RAISES_AT_QUERY: set[str] = set()


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


def _corrupt_a_page(good: bytes) -> bytes:
    """Zero page 2, keeping a valid header.

    SQLite opens this file happily — the header and page 1 are intact — and only
    discovers the damage when a query reaches the zeroed page. That is why it
    belongs here: `open_store` never sees it.
    """
    assert len(good) > 8192, "store too small to corrupt a page of"
    return good[:4096] + bytes(4096) + good[8192:]


#: The two damage shapes, and they fail in DIFFERENT PLACES, which is the whole
#: reason both are run:
#:
#:   at open  — the file is not a database at all; `open_store` raises
#:              StoreUnopenable and the root group translates it.
#:   at query — the file opens, and a SELECT reaches a zeroed page. The seam is
#:              already behind us; nothing catches this.
#:
#: This file measured only the first for its whole existence and reported the
#: class CLOSED on that evidence. An empty ratchet over one shape is a
#: strong-looking claim about a narrow universe — the same disease as a census
#: that cannot report its own blind spot, in the file built to prevent it.
DAMAGE = {
    "at-open": lambda good: CORRUPT,
    "at-query": _corrupt_a_page,
}


@pytest.fixture(params=sorted(DAMAGE), ids=sorted(DAMAGE))
def damaged_store(request: pytest.FixtureRequest, isolated_home: Path) -> str:
    """A machine that has been used, whose database is now damaged.

    Built by letting the CLI create the store and then damaging it, rather than
    by writing a file called context.db into an empty directory: the second is a
    machine that was never initialised, which is a different case with a
    different correct answer.

    Returns the shape's name so a test can ask which one it is running under —
    the two have different expectations while the query-time seam is open.
    """
    runner = CliRunner()
    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)
    database = paths.db_path()
    assert database.exists(), "the fixture did not produce a store to damage"
    shape: str = request.param
    database.write_bytes(DAMAGE[shape](database.read_bytes()))
    return shape


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
def test_a_damaged_store_never_produces_a_traceback(chain: list[str], damaged_store: str) -> None:
    """The property, one test per command so a failure names the command.

    Parametrised rather than looped: a loop reports the first failure and hides
    the rest, and the useful output here is WHICH commands are affected — that
    list is what tells whoever fixes it whether the right boundary is one
    command or `open_store`.
    """
    name = " ".join(chain)
    expected_to_raise = STILL_RAISES if damaged_store == "at-open" else STILL_RAISES_AT_QUERY
    raised = _raises_unhandled(chain)

    if name in expected_to_raise:
        assert raised is not None, (
            f"`aisquare {name}` no longer raises under {damaged_store} damage — "
            "good. Remove it from the matching STILL_RAISES set so the list keeps "
            "describing the truth; a ratchet that is not tightened is an allow list."
        )
        return

    assert raised is None, (
        f"`aisquare {name}` raised {type(raised).__name__} under {damaged_store} "
        f"damage: {raised}\n"
        "An unhandled exception reaches the operator as a Python traceback. A "
        "store that cannot be opened is a condition to report in one line, "
        "naming the file and what to do about it — not a crash."
    )


def _escaped(raised: BaseException | None) -> BaseException | None:
    """Whether `raised` is an exception that would reach the operator.

    THE RULE, SPLIT OUT FROM THE INVOCATION so a control can call it with a
    known-bad value. Both ratchets in this file are EMPTY, which means "nothing
    raises" is the expected answer — so a detector that has gone blind produces
    exactly the right result and every test passes. Measured: replacing the
    invocation with `raised = None` left all 200 cases green.

    That is the shape @9bbc8ed7 named: the checks watch the WALK — every command
    invoked, every allow-list entry real — while the RULE has stopped deciding
    anything. An empty ratchet makes it worse than usual, because emptiness is
    also what success looks like.

    `SystemExit` does not count: click raises it for usage errors and its own
    clean non-zero exits, and both are already legible.
    """
    if raised is None or isinstance(raised, SystemExit):
        return None
    return raised


def _raises_unhandled(chain: list[str]) -> BaseException | None:
    """The exception a command lets escape, or None."""
    return _escaped(CliRunner().invoke(app, chain, catch_exceptions=True).exception)


def test_the_ratchet_names_only_commands_that_exist() -> None:
    """Same hazard as a stale exclusion, on the other list.

    A renamed command would leave its old name here forever, silently claiming
    a defect that no longer has a home, while the renamed command itself walked
    out of coverage.
    """
    known = {" ".join(chain) for chain in _leaves()}

    unknown = sorted(name for name in STILL_RAISES if name not in known)

    assert not unknown, f"STILL_RAISES names commands that do not exist: {unknown}"


def test_the_class_is_closed_at_one_boundary(damaged_store: str) -> None:
    """This measured fourteen commands sharing one frame, so a fixer would know
    a single boundary could close the class. It could, and it did.

    Now it holds the other direction: NOTHING escapes. If a command starts
    raising again, ``frames`` is non-empty and this names the frame it escaped
    from — which says immediately whether the seam regressed or whether a new
    code path opens the store somewhere else.
    """
    frames: set[str] = set()
    for chain in _invoked():
        raised = _raises_unhandled(chain)
        if raised is None:
            continue
        traceback = raised.__traceback__
        while traceback is not None and traceback.tb_next is not None:
            traceback = traceback.tb_next
        if traceback is not None:
            frames.add(traceback.tb_frame.f_code.co_name)

    if damaged_store == "at-open":
        assert frames == set(), (
            f"a damaged store is escaping again, from: {sorted(frames)}. "
            "The seam is core.store.open_store raising StoreUnopenable and the "
            "root group translating it; a frame here means one of the two "
            "stopped."
        )
        return

    # It IS behind the seam now. This measured one shared frame — ``entries`` —
    # which is what said a single boundary could close the class; it could, and
    # it did. Held the other way round from here: nothing escapes at all, and a
    # frame named here says which side of the seam regressed.
    assert frames == set(), (
        f"query-time damage is escaping again, from: {sorted(frames)}. The seam "
        "is the root group translating a corrupt-file error that a query "
        "raised; a frame here means it stopped."
    )


def test_both_damage_shapes_are_run_and_are_genuinely_different() -> None:
    """Deleting a shape must fail, not quietly narrow the guard.

    This file spent its whole existence measuring one shape and reporting the
    class closed. Removing `at-query` from DAMAGE does not break any assertion
    above — everything simply passes over less, which is the failure mode that
    produced the blind spot in the first place. So the SET of shapes is asserted
    here, and so is the property that makes them worth running separately:

        at-open   the file is not a database; SQLite refuses to open it
        at-query  the file opens cleanly and a SELECT hits the damage

    If the two ever stop differing in that way, running both costs time and
    proves nothing extra, and this says so instead of leaving it to be noticed.
    """
    assert set(DAMAGE) == {"at-open", "at-query"}, (
        f"damage shapes are now {sorted(DAMAGE)}. A shape removed here narrows "
        "every assertion in this file without failing any of them."
    )

    good = b"SQLite format 3\x00" + bytes(9000)
    at_open = DAMAGE["at-open"](good)
    at_query = DAMAGE["at-query"](good)

    assert not at_open.startswith(b"SQLite format 3"), (
        "the at-open shape leaves a valid header, so SQLite would open it and "
        "the two shapes would be testing the same path"
    )
    assert at_query.startswith(b"SQLite format 3"), (
        "the at-query shape destroyed the header, so it fails at open like the "
        "other one and the query-time path is no longer covered"
    )
    assert len(at_query) == len(good), "the at-query shape truncated rather than corrupted"


def test_the_rule_still_recognises_an_escaping_exception() -> None:
    """POSITIVE control on the rule, which the empty ratchets make essential.

    With STILL_RAISES and STILL_RAISES_AT_QUERY both empty, every assertion in
    this file is satisfied by "no command raised" — and a rule that can no
    longer recognise an exception produces that answer for free. Nothing here
    could tell the two apart until this control existed.

    Synthetic exceptions rather than a command that really raises, because
    there is no longer one: the seam closed the class, which is precisely why
    the guard needs input it can no longer find in the wild.
    """
    escaping = sqlite3.DatabaseError("database disk image is malformed")

    assert _escaped(escaping) is escaping, "the rule no longer recognises an escape"
    assert _escaped(RuntimeError("anything unhandled")) is not None


def test_the_rule_still_ignores_the_exits_that_are_legible() -> None:
    """NEGATIVE control, so "call everything an escape" is not a fix.

    click raises SystemExit for a usage error and for its own clean non-zero
    exits. A rule that reported those would fail on every command that takes a
    required argument, and the guard would be deleted rather than repaired.
    """
    assert _escaped(SystemExit(2)) is None, "a usage error is being called a traceback"
    assert _escaped(None) is None
