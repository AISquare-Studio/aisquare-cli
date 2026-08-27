"""A real explainability-proxy ``/health`` endpoint, for tests that can't stub.

``wire_session`` binds its prober as a DEFAULT ARGUMENT, so monkeypatching
``services.explainability.probe_proxy`` never reaches it. That is fine for the
tests that only need to know the launcher forwards what the service returned —
but the correlation spine's central claim ("the id the agent is STARTED on is
the id in ``X-Pipeline-Id``") is a property of the whole chain, and stubbing
the middle of it would prove nothing. Serving the real payload on a loopback
port is the honest way to exercise it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

#: What the claude_code proxy answers with — service and mode are both part of
#: the contract ``probe_proxy`` checks, so both have to be real here.
HEALTHY: dict[str, str] = {"status": "ok", "service": "aisquare-proxy", "mode": "claude_code"}


class _HealthHandler(BaseHTTPRequestHandler):
    payload: ClassVar[dict[str, str]] = HEALTHY

    def do_GET(self) -> None:
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        return


@contextmanager
def healthy_proxy(payload: dict[str, str] | None = None, port: int = 0) -> Iterator[str]:
    """Serve ``payload`` on a loopback port and yield its base URL.

    ``port`` defaults to 0 — an ephemeral port, which is what tests want, since
    9090 is documented as somebody else's long-running proxy. CI's
    hostile-environment job passes an explicit port because reproducing that
    exact condition is its whole purpose.
    """
    handler = type("Handler", (_HealthHandler,), {"payload": payload or HEALTHY})
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _serve_forever(port: int) -> None:
    """Hold the port until killed. The entry point CI's hostile job uses.

    Exists so the workflow reuses THIS module's payload rather than inlining its
    own JSON: `probe_proxy` checks `service` and `mode`, so a hand-rolled stub in
    a YAML file would be a second copy of a contract that has to match, and the
    copy nobody runs locally is the one that drifts.
    """
    handler = type("Handler", (_HealthHandler,), {"payload": HEALTHY})
    HTTPServer(("127.0.0.1", port), handler).serve_forever()


if __name__ == "__main__":  # pragma: no cover - a CI entry point, not a test path
    import sys

    _serve_forever(int(sys.argv[1]) if len(sys.argv) > 1 else 9090)
