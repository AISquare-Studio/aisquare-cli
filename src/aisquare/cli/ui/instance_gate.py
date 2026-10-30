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
small folder and one ``ps``: no prompt, no wait.

An asq of a release before the registry (0.9.1 and older) writes no record: the gate finds
it in ``ps`` (``core.instance_scan``, #21) and refuses beside it as beside one that cannot
be asked to quit.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import typer

from aisquare.core import instance_scan, instances, paths
from aisquare.core.console import stderr_console
from aisquare.core.instance_scan import UnregisteredUi
from aisquare.core.instances import Instance, Unreadable

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

_SAID_UNCHECKED: set[Path] = set()
"""The homes this process has said it could not check: said once each."""


def _say(line: str) -> None:
    stderr_console().print(line, markup=False, highlight=False)


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):  # a detached or closed stream
        return False


def _confirm(question: str) -> bool:
    """The y/N question; Ctrl-C or the end of input is a no.

    ``typer.Abort``, never click's: typer 0.27 vendors click, and the ``Abort`` its
    ``confirm`` raises is typer's own, no subclass of click's, so an ``except`` on click's
    let it out as a bare "Aborted." with no word of how to quit the other asq (tester's
    reopen of #17). Before 0.27, ``typer.Abort`` was click's, so this catches either.
    """
    try:
        return typer.confirm(question, default=False)
    except typer.Abort:
        typer.echo()  # the prompt's line ends here, not in the refusal said under it
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
    unreadable: list[Unreadable],
    strays: list[UnregisteredUi],
    *,
    ask: Ask | None,
    interactive: bool | None,
    wait_s: float,
) -> None:
    """Say why ``me`` does not start beside ``differing``, ``unreadable`` and ``strays``;
    return once they have quit on a yes, or raise :class:`typer.Exit` (1). An unreadable record
    (an asq of an unknown release) and a stray (an asq of a release before the registry, found
    in ``ps``, #21) cannot be asked to quit, so with either there is nothing to ask."""
    _say(f"⚠ {_release(me)} cannot run beside a running asq of another release:")
    for other in differing:
        _say(f"  {_release(other)} — {other.where}")
    for record in unreadable:
        written = record.written_at.astimezone().strftime("%H:%M:%S")
        _say(
            f"  asq of a release this one cannot read — its record {record.path}, written {written}"
        )
    for stray in strays:
        _say(f"  asq of a release before the registry — {stray.where}: {stray.shown_command}")
    _say("Two releases on one home would run two codebases against one store and one tmux.")
    unaskable = [other for other in differing if not other.askable]
    if unaskable or unreadable or strays:
        for other in unaskable:
            on = "" if other.host == me.host else f" on {other.host}"
            _say(
                f"asq {other.version} (pid {other.pid}) predates being asked to quit, so it "
                f"cannot be asked from here. Quit it in its own terminal (q), or end it with "
                f"`kill {other.pid}`{on}: {SIGTERM_LOSES}."
            )
        for record in unreadable:
            _say(
                f"The asq whose record is {record.path.name} cannot be asked to quit from here: "
                f"quit it in its own terminal (q). If no asq runs, the record is a leftover, "
                f"ignored once a minute old; or delete it."
            )
        for stray in strays:
            _say(
                f"The asq of pid {stray.pid} predates the registry: its release is unknown, and "
                f"it cannot be asked to quit from here. Quit it in its own terminal (q), or end "
                f"it with `kill {stray.pid}`: {SIGTERM_LOSES}."
            )
        those = len(unaskable) + len(unreadable) + len(strays) > 1
        raise _refuse(f"Start it again once {'those asqs have' if those else 'that asq has'} quit.")
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


def _accounted(found: instances.Roll, me: Instance) -> set[int]:
    """The pids the registry accounts for, which the look in ``ps`` must not take for asqs
    before the registry: every live record's on this host, and every live record's whose lock
    is held whatever host it names (macOS renames the host with the network when no HostName
    is set, #21's reopen), and the pid an unreadable record still says."""
    pids = {other.pid for other in found.live if other.host == me.host or other.id in found.held}
    return pids | {record.pid for record in found.unreadable if record.pid is not None}


def _pass(
    *, ask: Ask | None, interactive: bool | None, wait_s: float
) -> instances.Registration | None:
    me = instances.describe_this_process()
    while True:
        # The look in ps runs BEFORE the gate's lock is taken: its calls (one, and one more per
        # asq UI of another home) are bounded at 2 s each, and held under the lock they could
        # hold an asq starting beside this one past GATE_WAIT_SECONDS, to be refused (#21's
        # reopen). What it found is filtered under the lock, against the registry as read
        # there: an asq that registered meanwhile is in that read, and one before the registry
        # started meanwhile is too young to be found anyway (instance_scan.STARTING_SECONDS).
        found_in_ps = instance_scan.unregistered_uis(_accounted(instances.roll(prune=False), me))
        try:
            with instances.gate() as found:
                differing = [other for other in found.live if other.differs_from(me)]
                accounted = _accounted(found, me)
                strays = [ui for ui in found_in_ps if ui.pid not in accounted]
                if any(record.pid is None for record in found.unreadable):
                    # A record this release cannot read, whose pid cannot be read either: its own
                    # asq may be among these, and is no release before the registry. The refusal
                    # names the record alone (#466's review).
                    strays = []
                if not differing and not found.unreadable and not strays:
                    return instances.register(me)
        except TimeoutError:
            # Another asq's gate held past its wait: started now, this one would run unchecked
            # and, unregistered, unseen by every asq after it (review of #462).
            raise _refuse(
                f"Another asq is checking this home and has held its gate over "
                f"{instances.GATE_WAIT_SECONDS:g} s. Start asq again in a moment."
            ) from None
        except OSError as exc:
            # A home asq cannot write in: it runs, as it did before there was a registry, and
            # says what that costs, once (`ui --project` passes the gate before run_ui does).
            # The only case the gate lets an asq through unchecked.
            home = paths.aisquare_home()
            if home not in _SAID_UNCHECKED:
                _SAID_UNCHECKED.add(home)
                _say(f"⚠ asq could not check the other asqs on this home ({exc}); none will see it")
            return None
        _quit_others(
            me,
            differing,
            found.unreadable,
            strays,
            ask=ask,
            interactive=interactive,
            wait_s=wait_s,
        )


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
    return _pass(ask=ask, interactive=interactive, wait_s=wait_s)
