"""What every remote test builds on (SPEC §0.3): a runtime with a known password, a
client that sends the page's ``Origin``, unlock, the routes of a built app, and a
socket read that fails instead of hanging.

The ``Origin`` header goes on every request and every handshake: the Origin
gate refuses a write or a socket without it (SPEC §2.8), so a test that is
about something else never trips it. Starlette's ``websocket_connect`` sends
the client's default headers too.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any
from urllib.parse import urlsplit

from starlette.routing import BaseRoute, Mount
from starlette.testclient import TestClient

from aisquare.core.paths import remote_audit_path, remote_state_path
from aisquare.services.remote_server import Runtime

ORIGIN = "http://testserver"
"""The origin of a page served by ``TestClient`` at its default base URL."""

PASSWORD = "amber-birch-cedar-delta"


def make_runtime() -> Runtime:
    """A runtime over the isolated home's ``remote.json``, its password :data:`PASSWORD`."""
    runtime = Runtime(remote_state_path(), remote_audit_path())
    runtime._state.password = PASSWORD
    runtime._save_state()
    return runtime


def make_client(app: Any, **kw: Any) -> TestClient:
    """A client that sends the page's ``Origin``: :data:`ORIGIN`, or ``base_url``'s own."""
    origin = ORIGIN
    if "base_url" in kw:
        parts = urlsplit(str(kw["base_url"]))
        origin = f"{parts.scheme}://{parts.netloc}"
    headers = {"origin": origin, **dict(kw.pop("headers", None) or {})}
    return TestClient(app, headers=headers, **kw)


def base(runtime: Runtime) -> str:
    return f"/r/{runtime.token}"


def unlock(client: TestClient, runtime: Runtime, password: str = PASSWORD) -> Any:
    return client.post(f"{base(runtime)}/api/unlock", json={"password": password})


def mounted_routes(app: Any) -> list[BaseRoute]:
    """Every route under ``/r/{token}`` of a built app, in the order they are matched."""
    mount = next(route for route in app._app.routes if isinstance(route, Mount))
    return list(mount.routes)


def receive_within(ws: Any, seconds: float = 10.0) -> dict[str, Any]:
    """The socket's next ASGI message (a frame or the close); a failed test after ``seconds``.

    The test session's own ``receive`` waits forever, and a stream that ends
    without a close frame sends nothing more: a test about how a stream ends
    would hang instead of failing. The wait runs on a daemon thread, which the
    session's exit releases.
    """
    got: list[dict[str, Any]] = []

    def wait() -> None:
        with contextlib.suppress(Exception):
            got.append(ws.receive())

    waiter = threading.Thread(target=wait, daemon=True)
    waiter.start()
    waiter.join(seconds)
    assert got, f"nothing from the socket in {seconds} s: did the stream end without a close?"
    return got[0]
