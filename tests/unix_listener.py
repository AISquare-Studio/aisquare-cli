"""A unix socket that answers as tmux's server does: every connect accepted.

Not a test module. A test that stands a bare ``listen(1)`` socket in for a tmux server
never accepted, so each probe stayed in the backlog. macOS refuses a connect once that
backlog is full (Linux queues it), so the second probe of a "live" socket was refused and
read as a stale file, no server: the plan's probe and then the run's, or two checks in
one test. A probe connects and closes (``core.tmux.socket_answer``), so accepting is all
a stand-in server owes it.
"""

from __future__ import annotations

import socket
import threading
from contextlib import suppress


class Accepting:
    """``server``, bound and listening, accepting every connect on a thread of its own
    until :meth:`close`, which stops the thread and then closes the socket, leaving its
    file as tmux leaves its socket's when its server exits."""

    def __init__(self, server: socket.socket) -> None:
        self._server = server
        self._done = threading.Event()
        # A full backlog too: a probe queued while the thread is late is not refused, as two
        # probes back to back were on a loaded Mac before the thread had taken the first.
        server.listen(socket.SOMAXCONN)
        server.settimeout(0.05)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._done.is_set():
            try:
                self._server.accept()[0].close()
            except TimeoutError:
                continue
            except OSError:
                return  # closed under it

    def close(self) -> None:
        self._done.set()
        with suppress(RuntimeError):
            self._thread.join()
        self._server.close()
