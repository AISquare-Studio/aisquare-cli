"""A proxy that HANGS is the shape no test exercises, and the one that stalls.

Every existing test drives the probe at a refused connection — nothing
listening, which fails instantly. A black hole is different: it completes the
TCP handshake and then never answers. That is the shape a wedged proxy, a
half-dead container or a DROP firewall rule produces, and it is the only one
that can make a launch sit there.

MEASURED AT ecf12cc, a measurement at a commit rather than a bound to assert:

    refused (nothing listening)              exit 1   351 ms
    BLACK HOLE (accepts TCP, never replies)  exit 1  1859 ms
    black hole, second run                   exit 1  1841 ms
    healthy stub                             exit 0   332 ms

``_PROBE_TIMEOUT_SECONDS`` is 1.5 and it is genuinely enforced: ~1.5s on top of
a ~0.35s baseline, then the wiring fails open with the reason on stderr and the
session launches untraced. It costs a trace and a second and a half, never the
launch — which is the doctrine working, not a defect.

WHAT THIS ASSERTS AND WHY IT IS SHAPED THIS WAY. Not "fast": a wall-clock bound
near the real value flakes on a loaded box and a muted test is worse than none.
It asserts BOUNDED — that the call RETURNS, well inside a generous margin. That
still catches the regression that matters, which is the timeout being raised or
dropped so urllib falls back to its own default and a misconfigured machine
stalls with nothing failing and a human deciding the CLI is hung.

The port is always OS-assigned. A test with a hardcoded port is a test that
will one day be pointed at port 9090, which on this machine holds a long-lived
proxy that is not ours.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from aisquare.services.explainability import (
    _PROBE_TIMEOUT_SECONDS,
    ProxyProbe,
    probe_proxy,
)

#: Generous on purpose. The real cost is ~1.5s; this fails only if the timeout
#: stopped being applied at all, which is the regression worth catching.
_GENEROUS_BOUND_SECONDS = 8.0


@contextmanager
def _black_hole() -> Iterator[str]:
    """A listener that accepts and never answers, on an OS-assigned port.

    Accepting matters: a CLOSED port is refused instantly and would satisfy the
    assertion below for entirely the wrong reason. The handshake has to
    succeed so the probe is waiting on a READ, which is what the timeout
    governs.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept_forever() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                continue
            held.append(connection)  # kept open, deliberately unanswered

    worker = threading.Thread(target=accept_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop.set()
        worker.join(timeout=2)
        for connection in held:
            connection.close()
        server.close()


@contextmanager
def _healthy_proxy() -> Iterator[str]:
    """The control: the same harness able to produce a HEALTHY verdict.

    Without it, "the probe reported unhealthy" is indistinguishable from a
    harness that can never produce anything else.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"service": "aisquare-proxy", "mode": "claude_code"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _probe_with_watchdog(url: str, bound: float) -> tuple[ProxyProbe, float]:
    """Run the probe on a thread so a probe that never returns FAILS the test.

    Sabotage found this: with the ``timeout=`` argument dropped, urllib waits
    indefinitely, and a test that simply calls the probe and then asserts on
    the elapsed time NEVER REACHES ITS ASSERTION. It hangs — which in CI is a
    stuck build rather than a red one, and is barely better than passing. The
    thread is abandoned rather than joined on failure, because there is nothing
    to cancel; the assertion is what matters.
    """
    result: list[ProxyProbe] = []
    started = time.monotonic()

    def run() -> None:
        result.append(probe_proxy(url))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=bound)
    elapsed = time.monotonic() - started
    if not result:
        raise AssertionError(
            f"the probe did not return within {bound}s against a hanging proxy. "
            f"_PROBE_TIMEOUT_SECONDS is {_PROBE_TIMEOUT_SECONDS}; a launch on a "
            "misconfigured machine now stalls indefinitely."
        )
    return result[0], elapsed


def _elapsed_with_watchdog(url: str, *, timeout: float, bound: float) -> float:
    """Same watchdog, for a probe called with an explicit timeout.

    This test needed it for a reason the first sabotage exposed only indirectly:
    with the ``timeout=`` argument dropped from the source, THIS test hung too,
    and a hung test anywhere in the file stalls the whole run — which is why the
    first sabotage looked like it produced no output at all. Every call that can
    hang gets the watchdog, not just the one the file is named after.
    """
    done: list[float] = []
    started = time.monotonic()

    def run() -> None:
        probe_proxy(url, timeout=timeout)
        done.append(time.monotonic() - started)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=bound)
    if not done:
        raise AssertionError(
            f"a probe with timeout={timeout}s did not return within {bound}s — "
            "the argument is not being applied"
        )
    return done[0]


def test_a_hanging_proxy_does_not_hang_the_probe() -> None:
    """The regression bound: it must RETURN, not return quickly."""
    with _black_hole() as url:
        verdict, elapsed = _probe_with_watchdog(url, _GENEROUS_BOUND_SECONDS)

    assert verdict.healthy is False
    assert elapsed < _GENEROUS_BOUND_SECONDS, (
        f"the probe took {elapsed:.1f}s against a hanging proxy. The timeout is "
        f"{_PROBE_TIMEOUT_SECONDS}s; a launch on a misconfigured machine now "
        "stalls, and nothing else in the suite exercises this shape."
    )
    # LOWER bound, and it is the fixture's own control. Sabotage found that a
    # black hole degraded into a REFUSING port still passed everything above —
    # refused also returns unhealthy, in about five milliseconds. Only a real
    # hang makes the probe WAIT, so waiting is the proof the fixture is still
    # the shape this file is about.
    assert elapsed >= _PROBE_TIMEOUT_SECONDS / 2, (
        f"the probe returned in {elapsed:.3f}s, far too fast to have waited on a "
        "timeout — the listener is refusing connections rather than holding "
        "them open, so this test is no longer exercising a hang"
    )


def test_the_harness_can_also_produce_a_healthy_verdict() -> None:
    """The control. An unhealthy result means nothing without it."""
    with _healthy_proxy() as url:
        verdict = probe_proxy(url)

    assert verdict.healthy is True, verdict.reason


def test_the_timeout_is_actually_consulted() -> None:
    """Bounded is not the same as governed by the constant.

    A shorter timeout must produce a shorter wait against the same black hole.
    This is what distinguishes "the timeout is applied" from "something else
    happens to return in time" — the assertion above cannot tell those apart.
    """
    with _black_hole() as url:
        quick = _elapsed_with_watchdog(url, timeout=0.25, bound=_GENEROUS_BOUND_SECONDS)

    assert quick < _PROBE_TIMEOUT_SECONDS, (
        f"a 0.25s timeout waited {quick:.2f}s, which is not shorter than the "
        f"{_PROBE_TIMEOUT_SECONDS}s default — the argument is being ignored"
    )
