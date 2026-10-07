"""Settling the fleet UI's workers in a test: one definition for every module that needs it.

Not a test module. ``tests/test_ui_accounts.py`` and ``tests/test_ui_project.py``
each carried a copy, and the copies drifted: the project page's used
``workers.wait_for_complete()``, which RAISES for a worker that errored, and
failed now and then on windows-latest before reaching its assertion, while the
accounts page's copy had already learned the race (review of #65, R11).

``settle_page`` is here for the same reason. The loop was the project page's
alone, and the accounts page, which waited only for the workers that existed,
lost the race it closes on windows-latest.

``settle_until`` is that settle, repeated until the fact a test is about to
assert holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

from textual.app import App
from textual.css.query import NoMatches
from textual.pilot import Pilot
from textual.worker import Worker, WorkerError


async def settle_workers(app: App[Any], *, group: str | None = None) -> None:
    """Wait for every worker of ours to reach a terminal state, whatever that state is.

    NOT ``workers.wait_for_complete()``, which raises for a worker that ERRORED.
    An errored worker is often the designed outcome: a scripted ``IamError`` or
    ``FleetUnavailable`` is what the test goes on to read off the page. The raise
    needs the worker to still be registered when it is sampled, so it failed only
    when the thread lost a race, which a loaded runner loses more often. Each
    worker is awaited on its own and its failure swallowed; the page shows the
    result. The ``_loader`` group is the app's own and is left running. With
    ``group``, only that group's workers are waited for (see ``settle_page``).
    """
    for worker in list(app.workers):
        if not _ours(worker, group):
            continue
        with contextlib.suppress(WorkerError):
            await worker.wait()


async def settle_page(app: App[Any], *, group: str | None = None) -> None:
    """Let the page go quiet: nothing queued on the app or its screen, every worker of ours done.

    ``settle_workers`` alone waits for the workers that exist when it is called,
    and a test that pauses once after it trusts that pause for the rest. The pause's
    idle check is a guess from CPU use, which counts a thread waiting on a file as
    idle, and a bubbling message is queued on each parent behind the pause's own
    callback, so a handler can still be pending when the pause returns. Three ways
    that loses:

    - the handler that STARTS a worker has not run yet. The Accounts page starts
      its usage reading from ``on_show``, and a ``Show`` still queued when its
      test settled left no worker to wait for: the reading started in the pause
      after, and on windows-latest the test read no usage at all (CI run
      36078630575);
    - a worker that a finishing worker's state-change handler starts is not in
      the snapshot, so it is never waited for (review of the accounts stack's
      fold, round 2, F8);
    - a message being HANDLED is in no queue. A handler that awaits, as the app's
      ``on_agent_selected`` awaits the mount of the view it opens, can take its
      message off the queue during the pause's idle check and still be running
      when the pause returns. Every queue was empty, the test went on, and
      ``run_test`` shut the app down under the half-mounted view
      (``NoMatches('#agent-stop')`` with every message held 20 ms).

    So it goes round: pause, then wait for what is running, until a pause ends
    with no message queued on the app or its screen, none still in hand there,
    and no worker of ours unfinished. The rounds are bounded, so a page that
    never goes quiet fails at its test's assertion, not here. The screen is the
    CURRENT one, as in Textual's own wait: a screen under a modal is not counted
    while the modal is up, and each round reads the current screen again, so the
    round after a dismiss covers the screen beneath. The pauses are
    ``Pilot.pause``; a ``Pilot`` holds nothing but its app (``run_test`` builds
    its own the same way), so this takes the app, as ``settle_workers`` does.

    With ``group``, the workers waited for are that group's alone, and the
    messages are still every node's. A test that holds a worker on purpose (the
    Accounts page's credits reading, held while the test reads the page) would
    otherwise wait for it here and never return: ``tests/test_ui_accounts.py``'s
    ``accounts_read`` waits for the shell's accounts read this way, the one
    worker that fills the page in after a tick (final review of #203, accounts
    F2), with the same rounds.
    """
    pilot = Pilot(app)
    for _ in range(_SETTLE_ROUNDS):
        await pilot.pause()
        if not _busy(app, group):
            await _handled(app)
            if not _busy(app, group):
                return
        await settle_workers(app, group=group)


async def settle_until(
    app: App[Any],
    done: Callable[[], object],
    *,
    group: str | None = None,
    timeout: float = 10.0,
) -> None:
    """Settle the page, round after round, until ``done()`` holds or ``timeout`` seconds pass.

    A test that settles and then asserts trusts the settle to have seen everything
    the page was going to do. A test that asserts what a worker did (the Accounts
    page's usage reading recorded, its row painted) names that fact here instead,
    and reads the page once it holds, however late the worker started. A widget
    that ``done`` reads and that is not mounted yet is a fact that does not hold
    yet.

    Bounded and quiet, as ``settle_page`` is: when ``timeout`` passes it returns,
    and the test's own assertion fails with what the page showed. ``group`` is
    passed on to ``settle_page``, for a test that holds another worker on purpose.
    """
    deadline = time.monotonic() + timeout
    while True:
        await settle_page(app, group=group)
        with contextlib.suppress(NoMatches):
            if done():
                return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(_UNTIL_POLL_S)


_SETTLE_ROUNDS = 20
_HANDLED_S = 5.0
_UNTIL_POLL_S = 0.02


async def _handled(app: App[Any]) -> None:
    """Wait until the app and every node of its screen is done with the message in hand.

    A callback queued on a node runs once the node has finished what it is handling:
    the wait ``Pilot.pause`` opens with, here without the idle guess after it, so a
    quiet app costs next to nothing. Bounded, as Textual bounds its own wait; a node
    that has not answered by then is left to the check after this, and to the next
    round.
    """
    asked: list[asyncio.Event] = []
    for node in [app, *app.screen.walk_children(with_self=True)]:
        done = asyncio.Event()
        if node.call_later(done.set):
            asked.append(done)
    if asked:
        waits = [asyncio.create_task(done.wait()) for done in asked]
        _, pending = await asyncio.wait(waits, timeout=_HANDLED_S)
        for task in pending:
            task.cancel()


def _busy(app: App[Any], group: str | None = None) -> bool:
    """Whether a message is queued on the app or its screen, or a worker of ours runs.

    The ``_loader`` group is the app's own and is left running, as ``settle_workers``
    leaves it.
    """
    nodes = [app, *app.screen.walk_children(with_self=True)]
    return any(node.message_queue_size for node in nodes) or any(
        not worker.is_finished for worker in app.workers if _ours(worker, group)
    )


def _ours(worker: Worker[Any], group: str | None) -> bool:
    """Whether a settle waits for ``worker``: never the app's own ``_loader``, and with
    ``group``, only that group's."""
    return worker.group != "_loader" and (group is None or worker.group == group)
