"""A Remote on for this home in another process, as ``upgrade`` and ``uninstall`` find one.

Its lock, ``remote-serve.lock``, is held through a descriptor of the test's own, which
conflicts with any other as another process's does (``tests/test_remote_control.py``'s
``another_process_serves``), and the note its claim writes beside it says who it is
(``services.remote_server.remote_served_by``). Only the standard library and the paths
module, so a test of the install's lifecycle needs none of the ``remote`` extra.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

from aisquare.core import paths
from aisquare.core.locking import lock_exclusive, unlock
from aisquare.services import remote_server


def remote_lock() -> Path:
    """This home's ``remote-serve.lock``. While :func:`remote_on_elsewhere` holds it, Windows
    refuses to read its bytes to any other descriptor, as it would to another process."""
    return paths.remote_state_path().with_name(remote_server.SERVE_LOCK_NAME)


@contextlib.contextmanager
def remote_on_elsewhere(*, by: str = "serve", port: int = 8750, pid: int = 4242) -> Iterator[str]:
    """Hold this home's Remote lock as the process ``pid`` that claimed it ``by`` on ``port``
    does; yields how ``remote_served_by`` names it."""
    paths.ensure_home()
    lock = remote_lock()
    lock.with_name(remote_server.SERVE_NOTE_NAME).write_text(
        json.dumps({"pid": pid, "by": by, "port": port}), encoding="utf-8"
    )
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        lock_exclusive(fd)
        try:
            yield f"{remote_server.SERVED_BY[by]} on port {port}, process {pid}"
        finally:
            unlock(fd)
    finally:
        os.close(fd)
