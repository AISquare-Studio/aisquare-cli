"""A local CI server that speaks hook contract v2, for the client tests.

The real ``/v1/hook`` is not built, so every client test runs against this. It
is deliberately a real socket rather than a mocked ``urlopen``: the failures
worth proving — a hung connection, a body that dribbles in past the ceiling, a
truncated body, a 401 on the descriptor, a 500 with an explanation in it — are
transport behaviour, and a mock that returns them is only testing the mock's
idea of what urllib does.

Three routes, matching the server's:

- ``GET /ready`` — always 200 (``doctor``'s reachability probe);
- ``GET /v1/experiment/runs/{run_id}`` — the delivery descriptor, programmable
  (default: the vendored valid fixture, so the stub and the contract agree);
- ``POST /v1/hook`` — programmable status, body, delay before headers, and a
  drip (``chunks`` pieces, ``interval`` seconds apart) for the deadline tests.

Every request is recorded with its method, path, headers and parsed body.

Run it by hand to point a real Claude Code session at it::

    python -m tests.stub_ci_server --port 8765

and export ``AISQUARE_CI=1 AISQUARE_CI_URL=http://127.0.0.1:8765
AISQUARE_CI_KEY=x AISQUARE_CI_RUN=run_kernel0001`` in the shell that launches
the agent. It prints every hook request it receives.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "ci_contract" / "v2"


def fixture(name: str) -> dict[str, Any]:
    """A vendored fixture as a dict (``hook-response.experimental-v2.valid`` …)."""
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def fixture_text(name: str) -> str:
    """A vendored fixture's exact bytes, as text."""
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


FAR_FUTURE = "2099-01-01T00:00:00Z"


def error_v1(code: str, http_status: int, message: str, **detail: Any) -> dict[str, Any]:
    """An ``error.v1`` body the way the live server writes one on a non-200.

    The vendored fixture pins the shape; this fills it the way
    ``app/api/errors.py`` does for a delivery refusal — ``scope_resolution_failed``
    on a 401, ``dependency_unavailable`` on a 503 — so the client's reading of a
    real refusal is exercised without the real server.
    """
    return {
        "schema_version": "error/v1",
        "code": code,
        "http_status": http_status,
        "message": message,
        "subject_ref": None,
        "retryable": False,
        "detail": detail,
        "occurred_at": "2026-09-02T14:59:10.866894Z",
    }


def live_descriptor(**overrides: Any) -> dict[str, Any]:
    """The vendored descriptor with an expiry that has not passed.

    The fixture's own ``expires_at`` is 2026-08-22, which the client correctly
    refuses as expired — so a stub serving the bytes verbatim would test the
    expiry path and nothing else. Only the expiry (and whatever ``overrides``
    name) differs from the server's bytes.
    """
    descriptor = fixture("client-delivery-descriptor.v1.valid")
    descriptor["expires_at"] = FAR_FUTURE
    descriptor.update(overrides)
    return descriptor


@dataclass
class Behaviour:
    """What the stub does with the next ``POST /v1/hook``."""

    status: int = 200
    body: str = field(default_factory=lambda: fixture_text("hook-response.experimental-v2.valid"))
    delay_s: float = 0.0
    """Held before the headers, to exercise a stalled endpoint."""
    drip: tuple[int, float] | None = None
    """``(chunks, interval_s)``: send the body in pieces this far apart, so the
    per-socket-op timeout never fires while the wall clock runs out."""


@dataclass
class Recorded:
    """One request the stub saw."""

    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any] | None
    raw: str


@dataclass
class StubCI:
    """A running stub server plus everything it saw."""

    url: str
    behaviour: Behaviour
    descriptor_status: int = 200
    descriptor_body: str = field(default_factory=lambda: json.dumps(live_descriptor()))
    ready_status: int = 200
    seen: list[Recorded] = field(default_factory=list)
    echo: bool = False

    @property
    def requests(self) -> list[dict[str, Any]]:
        """Parsed bodies of every ``POST /v1/hook``, in order."""
        return [r.body if r.body is not None else {"__unparsed__": r.raw} for r in self.hooks]

    @property
    def hooks(self) -> list[Recorded]:
        return [r for r in self.seen if r.method == "POST"]

    @property
    def headers(self) -> list[dict[str, str]]:
        """Headers of every ``POST /v1/hook``, in order."""
        return [r.headers for r in self.hooks]

    @property
    def call_count(self) -> int:
        """How many hook calls arrived. Descriptor and readiness GETs are not calls."""
        return len(self.hooks)

    @property
    def descriptor_fetches(self) -> int:
        return sum(1 for r in self.seen if r.method == "GET" and "/v1/experiment/runs/" in r.path)

    def respond(
        self,
        *,
        status: int = 200,
        body: str = "",
        delay_s: float = 0.0,
        drip: tuple[int, float] | None = None,
    ) -> None:
        """Set what the next hook call gets back."""
        self.behaviour.status = status
        self.behaviour.body = body
        self.behaviour.delay_s = delay_s
        self.behaviour.drip = drip

    def respond_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self.respond(status=status, body=json.dumps(payload))

    def descriptor_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        """Set the descriptor every run id resolves to."""
        self.descriptor_status = status
        self.descriptor_body = json.dumps(payload)


def _handler(stub: StubCI) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _record(self, raw: str) -> Recorded:
            try:
                parsed: dict[str, Any] | None = json.loads(raw) if raw else None
                if not isinstance(parsed, dict):
                    parsed = None
            except json.JSONDecodeError:
                parsed = None
            record = Recorded(
                method=self.command,
                path=self.path,
                headers=dict(self.headers.items()),
                body=parsed,
                raw=raw,
            )
            stub.seen.append(record)
            if stub.echo:
                sys.stderr.write(f"{record.method} {record.path}\n{raw}\n\n")
                sys.stderr.flush()
            return record

        def _send(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._record("")
            if self.path == "/ready":
                self._send(stub.ready_status, b'{"status": "ready"}')
                return
            if self.path.startswith("/v1/experiment/runs/"):
                self._send(stub.descriptor_status, stub.descriptor_body.encode("utf-8"))
                return
            self._send(404, b'{"error": "no such route"}')

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            self._record(raw)
            behaviour = stub.behaviour
            if behaviour.delay_s:
                time.sleep(behaviour.delay_s)
            payload = behaviour.body.encode("utf-8")
            if behaviour.drip is None:
                self._send(behaviour.status, payload)
                return
            chunks, interval = behaviour.drip
            self.send_response(behaviour.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            size = max(1, -(-len(payload) // max(1, chunks)))
            for start in range(0, len(payload), size):
                self.wfile.write(payload[start : start + size])
                self.wfile.flush()
                if start + size < len(payload):
                    time.sleep(interval)

        def log_message(self, *args: Any) -> None:
            """Silence the default stderr access log."""

    return Handler


def serve(*, port: int = 0, echo: bool = False) -> Iterator[StubCI]:
    """Run a stub server on ``port`` (0 = ephemeral) for the life of a test."""
    stub = StubCI(url="", behaviour=Behaviour(), echo=echo)
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(stub))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A hook-contract-v2 stub CI server.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    for stub in serve(port=args.port, echo=True):
        run = fixture("client-delivery-descriptor.v1.valid")["run_id"]
        print(f"stub CI server on {stub.url}", file=sys.stderr)
        print(
            f"export AISQUARE_CI=1 AISQUARE_CI_URL={stub.url} AISQUARE_CI_KEY=x "
            f"AISQUARE_CI_RUN={run}",
            file=sys.stderr,
        )
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - a human runs this
    raise SystemExit(main())
