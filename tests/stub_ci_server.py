"""A local CI endpoint with programmable behaviour, for client tests.

The real endpoint is not live, so every client test runs against this. It is
deliberately a real socket rather than a mocked ``urlopen``: the failures worth
proving — a hung connection, a truncated body, a 500 with an explanation in it
— are transport behaviour, and a mock that returns them is only testing the
mock's idea of what urllib does.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Behaviour:
    """What the stub does with the next request."""

    status: int = 200
    body: str = '{"contract": 1, "action": "noop"}'
    delay_s: float = 0.0
    """Held before responding, to exercise the client's backstop."""


@dataclass
class StubCI:
    """A running stub endpoint plus everything it saw."""

    url: str
    behaviour: Behaviour
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def respond(self, *, status: int = 200, body: str = "", delay_s: float = 0.0) -> None:
        """Set what the next request gets back."""
        self.behaviour.status = status
        self.behaviour.body = body
        self.behaviour.delay_s = delay_s

    def respond_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self.respond(status=status, body=json.dumps(payload))


def _handler(stub: StubCI) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                stub.requests.append(json.loads(raw))
            except json.JSONDecodeError:
                stub.requests.append({"__unparsed__": raw})
            stub.headers.append({key: value for key, value in self.headers.items()})

            if stub.behaviour.delay_s:
                threading.Event().wait(stub.behaviour.delay_s)

            payload = stub.behaviour.body.encode("utf-8")
            self.send_response(stub.behaviour.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            """Silence the default stderr access log."""

    return Handler


def serve() -> Iterator[StubCI]:
    """Run a stub endpoint on an ephemeral port for the life of a test."""
    behaviour = Behaviour()
    stub = StubCI(url="", behaviour=behaviour)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    server.daemon_threads = True
    stub.url = f"http://127.0.0.1:{server.server_address[1]}"
    # serve_forever polls for shutdown every 0.5s by default, which would
    # cost half a second of teardown per test in this module alone.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
