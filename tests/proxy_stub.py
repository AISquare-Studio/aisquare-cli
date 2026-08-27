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
def healthy_proxy(payload: dict[str, str] | None = None) -> Iterator[str]:
    """Serve ``payload`` on a loopback port and yield its base URL."""
    handler = type("Handler", (_HealthHandler,), {"payload": payload or HEALTHY})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
