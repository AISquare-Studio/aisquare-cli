"""Settling the fleet UI's workers in a test: one definition for every module that needs it.

Not a test module. ``tests/test_ui_accounts.py`` and ``tests/test_ui_project.py``
each carried a copy, and the copies drifted: the project page's used
``workers.wait_for_complete()``, which RAISES for a worker that errored, and
failed now and then on windows-latest before reaching its assertion, while the
accounts page's copy had already learned the race (review of #65, R11).
"""

from __future__ import annotations

import contextlib
from typing import Any

from textual.app import App
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
