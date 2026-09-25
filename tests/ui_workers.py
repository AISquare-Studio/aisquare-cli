"""Settling the fleet UI's workers in a test: one definition for every module that needs it.

Not a test module. ``tests/test_ui_accounts.py`` and ``tests/test_ui_project.py``
each carried a copy, and the copies drifted: the project page's used
``workers.wait_for_complete()``, which RAISES for a worker that errored, and
failed now and then on windows-latest before reaching its assertion, while the
accounts page's copy had already learned the race (review of #65, R11).

``settle_page`` is here for the same reason. The loop was the project page's
alone, and the accounts page, which waited only for the workers that existed,
lost the race it closes on windows-latest.
"""

from __future__ import annotations

import contextlib
from typing import Any

from textual.app import App
from textual.pilot import Pilot
from textual.worker import WorkerError


async def settle_workers(app: App[Any]) -> None:
    """Wait for every worker of ours to reach a terminal state, whatever that state is.

    NOT ``workers.wait_for_complete()``, which raises for a worker that ERRORED.
    An errored worker is often the designed outcome: a scripted ``IamError`` or
    ``FleetUnavailable`` is what the test goes on to read off the page. The raise
    needs the worker to still be registered when it is sampled, so it failed only
    when the thread lost a race, which a loaded runner loses more often. Each
    worker is awaited on its own and its failure swallowed; the page shows the
    result. The ``_loader`` group is the app's own and is left running.
    """
    for worker in list(app.workers):
        if worker.group == "_loader":
            continue
        with contextlib.suppress(WorkerError):
            await worker.wait()


async def settle_page(app: App[Any]) -> None:
    """Let the page go quiet: every message queued on it handled, every worker of ours done.

    ``settle_workers`` alone waits for the workers that exist when it is called,
    and a test that pauses once after it trusts that pause for the rest. The pause's
    idle check is a guess from CPU use, which counts a thread waiting on a file as
    idle, and a bubbling message is queued on each parent behind the pause's own
    callback, so a handler can still be pending when the pause returns. Two ways
    that loses:

    - the handler that STARTS a worker has not run yet. The Accounts page starts
      its usage reading from ``on_show``, and a ``Show`` still queued when its
      test settled left no worker to wait for: the reading started in the pause
      after, and on windows-latest the test read no usage at all (CI run
      36078630575);
    - a worker that a finishing worker's state-change handler starts is not in
      the snapshot, so it is never waited for (review of the accounts stack's
      fold, round 2, F8).

    So it goes round: pause, then wait for what is running, until a pause ends
    with no message queued on the app or its screen and no worker of ours
    unfinished. The rounds are bounded, so a page that never goes quiet fails at
    its test's assertion, not here. The pauses are ``Pilot.pause``; a ``Pilot``
    holds nothing but its app (``run_test`` builds its own the same way), so this
    takes the app, as ``settle_workers`` does.
    """
    pilot = Pilot(app)
    for _ in range(_SETTLE_ROUNDS):
        await pilot.pause()
        if not _busy(app):
            return
        await settle_workers(app)


_SETTLE_ROUNDS = 20


def _busy(app: App[Any]) -> bool:
    """Whether a message is queued on the app or its screen, or a worker of ours runs.

    The ``_loader`` group is the app's own and is left running, as ``settle_workers``
    leaves it.
    """
    nodes = [app, *app.screen.walk_children(with_self=True)]
    return any(node.message_queue_size for node in nodes) or any(
        not worker.is_finished for worker in app.workers if worker.group != "_loader"
    )
