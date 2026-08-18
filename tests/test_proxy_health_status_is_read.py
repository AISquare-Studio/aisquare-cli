"""The probe validates two identity fields and discards the one named ``status``.

Measured at 168a515 against the proxy live on this box (read-only GET, nothing
touched, and not port 9090)::

    {"status": "ok", "service": "aisquare-proxy", "mode": "claude_code",
     "governance": "gateway"}

    keys the CLI inspects : mode, service
    keys the CLI DISCARDS : governance, status

``probe_proxy``'s own docstring makes the argument and then stops one field
short: "A 200 ALONE IS NOT HEALTH HERE … so the service and mode names in the
payload are part of the check." The same sentence argues for ``status``. A
proxy answering ``{"status": "degraded"}`` with the right service and mode was
reported as "proxy healthy", and model traffic routes to it.

The runbook does not check it either: §3 prints that payload verbatim,
including ``"status":"ok"``, and then says "Both fields matter" about the other
two. Four fields shown, two said to matter.

CONSERVATIVE BY CONSTRUCTION, because this rests on ONE payload from ONE proxy
and the SDK's contract for the field is unknown to me:

    absent            -> healthy, unchanged (an older proxy must keep working)
    "ok"              -> healthy, unchanged
    anything else     -> unhealthy, naming the value

That can only tighten the check for a proxy explicitly saying it is not ok, so
no setup that works today can break.

``governance`` is deliberately NOT checked. Its contract is equally unknown to
me and the governance blocker belongs to the workspace, not to this probe —
guessing at a second field from the same single sample is the generalisation
this shift keeps catching.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aisquare.services.explainability import probe_proxy

#: Byte-for-byte the payload measured from the live proxy, so the healthy-path
#: control is anchored to reality rather than to what I think it sends.
_REAL_PAYLOAD = {
    "status": "ok",
    "service": "aisquare-proxy",
    "mode": "claude_code",
    "governance": "gateway",
}


@contextmanager
def _serving(payload: Mapping[str, object]) -> Iterator[str]:
    """A /health on an OS-assigned port. Never a fixed one, never 9090."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(payload).encode()
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


def test_the_real_payload_is_still_healthy() -> None:
    """The control. If this fails the change broke every working machine."""
    with _serving(_REAL_PAYLOAD) as url:
        verdict = probe_proxy(url)

    assert verdict.healthy is True, verdict.reason


def test_a_payload_without_status_is_still_healthy() -> None:
    """Backwards compatibility, and the reason the rule is absent-tolerant.

    A proxy build that never sends the field must keep working; treating
    "missing" as "unhealthy" would fail machines that are fine.
    """
    payload = {k: v for k, v in _REAL_PAYLOAD.items() if k != "status"}

    with _serving(payload) as url:
        verdict = probe_proxy(url)

    assert verdict.healthy is True, verdict.reason


@pytest.mark.parametrize("status", ["degraded", "starting", "error", ""])
def test_a_proxy_that_says_it_is_unwell_is_believed(status: str) -> None:
    """The defect: right service, right mode, and it told us it was unwell."""
    with _serving({**_REAL_PAYLOAD, "status": status}) as url:
        verdict = probe_proxy(url)

    assert verdict.healthy is False, (
        f"a proxy reporting status={status!r} was called healthy, and model "
        "traffic would route to it"
    )


def test_the_reason_names_the_status_it_saw() -> None:
    """ "Proxy unhealthy" with no value sends the operator to the wrong place."""
    with _serving({**_REAL_PAYLOAD, "status": "degraded"}) as url:
        verdict = probe_proxy(url)

    assert "degraded" in verdict.reason, verdict.reason


def test_the_identity_checks_still_come_first() -> None:
    """A wrong-service proxy must still fail as a wrong SERVICE.

    Ordering matters for the message: "answers as 'something-else'" tells the
    operator they pointed at the wrong process, where "status" would send them
    looking at a healthy proxy's internals.
    """
    with _serving({**_REAL_PAYLOAD, "service": "something-else", "status": "degraded"}) as url:
        verdict = probe_proxy(url)

    assert verdict.healthy is False
    assert "something-else" in verdict.reason, verdict.reason
