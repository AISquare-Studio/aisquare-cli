"""The instance gate: an asq whose release differs from a live one's starts nothing (#17).

The owner runs asq at the Mac and over SSH into the same Mac at once, on one home
(``core.instances``). Two asqs of one release share it as two windows onto one fleet. Two
of different releases would run two codebases against one store and one tmux: the newer's
migrations under the older's reads, two control clients speaking two versions to one
server. So ``run_ui`` passes this gate before it builds the app, and the gate makes no tmux
call, starts no control client, no Remote and no receiver socket, and writes nothing but
the registry: an asq refused here has touched nothing of the fleet.

Beside a live asq of another release (or store schema), the gate says both versions and
where the other runs, and asks ``Quit the other asq? [y/N]``. Yes asks it to quit through
its quit-request channel: it quits as ``q`` quits it (its saves flushed, its Remote
stopped, what it could not save said in ITS terminal), and once its record has gone, this
one starts. No, no terminal to ask in, an asq too old to be asked, or one that has not quit
within :data:`QUIT_WAIT_SECONDS`: nothing starts, the exit is 1, and what was said is how
to quit it by hand. Beside asqs of its own release, or none, the gate is one read of a
small folder: no prompt, no wait.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import click
import typer

from aisquare.core import instances
from aisquare.core.console import stderr_console
from aisquare.core.instances import Instance

QUIT_WAIT_SECONDS = 15.0
"""How long a yes waits for the other asq to quit: its own quit waits up to a few seconds
for its saves and its Remote's stop."""

QUESTION = "Quit the other asq?"

SIGTERM_LOSES = (
    "a SIGTERM skips its clean quit: what it has not saved yet (its theme, its layout) is "
    'lost, phones on its Remote are not told "Remote is off", work still running in it (a '
    "restart of the fleet) is cut off, and its terminal may need `reset`"
)
"""What ``kill <pid>`` costs an asq, against its own quit (``run_ui`` after ``app.run``)."""

Ask = Callable[[str], bool]


def _say(line: str) -> None:
    stderr_console().print(line, markup=False, highlight=False)


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):  # a detached or closed stream
        return False


def _confirm(question: str) -> bool:
    try:
        return typer.confirm(question, default=False)
    except click.exceptions.Abort:  # Ctrl-C or the end of input: no
        return False


def _release(instance: Instance) -> str:
    return f"asq {instance.version} (store schema {instance.schema})"


def _by_hand(instance: Instance) -> str:
    """How a person quits ``instance`` where it runs."""
    tty = f", {instance.tty}" if instance.tty else ""
    return f"press q in that asq ({instance.kind} on {instance.host}, pid {instance.pid}{tty})"


def _refuse(why: str) -> typer.Exit:
    _say(f"asq did not start. {why}")
    return typer.Exit(1)


def _quit_others(
    me: Instance,
    differing: list[Instance],
    *,
    ask: Ask | None,
    interactive: bool | None,
    wait_s: float,
) -> None:
    """Say why ``me`` does not start beside ``differing``; return once they have quit on a yes,
    or raise :class:`typer.Exit` (1)."""
    _say(f"⚠ {_release(me)} cannot run beside a running asq of another release:")
    for other in differing:
        _say(f"  {_release(other)} — {other.where}")
    _say("Two releases on one home would run two codebases against one store and one tmux.")
    unaskable = [other for other in differing if not other.askable]
    if unaskable:
        for other in unaskable:
            on = "" if other.host == me.host else f" on {other.host}"
            _say(
                f"asq {other.version} (pid {other.pid}) predates being asked to quit, so it "
                f"cannot be asked from here. Quit it in its own terminal (q), or end it with "
                f"`kill {other.pid}`{on}: {SIGTERM_LOSES}."
            )
        raise _refuse("Start it again once that asq has quit.")
    many = len(differing) > 1
    hand = "; ".join(_by_hand(other) for other in differing)
    if not (_interactive() if interactive is None else interactive):
        raise _refuse(f"No terminal to ask in: quit {'them' if many else 'it'} first — {hand}.")
    question = "Quit the other asqs?" if many else QUESTION
    if not (ask or _confirm)(question):
        raise _refuse(f"Quit {'them' if many else 'it'} first — {hand} — then start asq again.")
    for other in differing:
        _say(
            f"asking asq {other.version} (pid {other.pid}) to quit: it saves what it can, and "
            f"says what it could not in its own terminal…"
        )
        try:
            instances.request_quit(other, by=me)
        except OSError as exc:
            by_hand = _by_hand(other)
            raise _refuse(f"The request could not be written ({exc}); {by_hand}.") from None
    for other in differing:
        if not instances.wait_gone(other, wait_s):
            raise _refuse(
                f"asq {other.version} (pid {other.pid}) has not quit within {wait_s:g} s: it "
                f"may be finishing work it does not cut off (a restart of the fleet), which it "
                f"says on its screen. Quit it there — {_by_hand(other)} — then start asq again."
            )
        _say(f"✓ asq {other.version} (pid {other.pid}) quit")


def _pass(
    *, record: bool, ask: Ask | None, interactive: bool | None, wait_s: float
) -> instances.Registration | None:
    me = instances.describe_this_process()
    while True:
        try:
            with instances.gate() as live:
                differing = [other for other in live if other.differs_from(me)]
                if not differing:
                    return instances.register(me) if record else None
        except OSError as exc:
            # A home asq cannot write in, or another asq's gate held past its wait: asq
            # runs, as it did before there was a registry, and says what it costs.
            _say(f"⚠ asq could not check the other asqs on this home ({exc}); none will see it")
            return None
        _quit_others(me, differing, ask=ask, interactive=interactive, wait_s=wait_s)


def check(
    *, ask: Ask | None = None, interactive: bool | None = None, wait_s: float = QUIT_WAIT_SECONDS
) -> None:
    """The gate without the registration: for what must not run beside another release before
    ``run_ui`` does, ``aisquare ui --project``'s read of the store."""
    if instances.this_registration() is None:
        _pass(record=False, ask=ask, interactive=interactive, wait_s=wait_s)


def admit(
    *, ask: Ask | None = None, interactive: bool | None = None, wait_s: float = QUIT_WAIT_SECONDS
) -> instances.Registration | None:
    """Pass the gate and register this asq, or raise :class:`typer.Exit` (1) having said why.

    ``None`` when the registry could not be written (said). ``ask`` stands for the y/N
    question and ``interactive`` for whether there is a terminal to ask in: a test's seams.
    """
    existing = instances.this_registration()
    if existing is not None:
        return existing
    return _pass(record=True, ask=ask, interactive=interactive, wait_s=wait_s)
