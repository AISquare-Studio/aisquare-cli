"""The one choke point (SPEC §1.2), over EVERY route of the built app.

The routes are found by walking the app's ``Mount``, never listed by hand: a
lane's routes join every check below the moment that lane merges, so a route
that forgot the device, the body cap or the write gate fails here without
anyone writing a test for it.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.routing import Route
from starlette.testclient import WebSocketDenialResponse

from aisquare.services import remote_server
from aisquare.services.remote_server import (
    MAX_BODY_BYTES,
    NOT_WRITE_GATED,
    READ_ONLY_REASON,
    Runtime,
    Sources,
    Writes,
    build_app,
    remote_gate_body,
    write_endpoint_names,
)
from tests.remote_kit_helpers import base, make_client, make_runtime, mounted_routes, unlock

SERVICES = Path(remote_server.__file__).parent

SAMPLE = {"agent": "coder-1", "device_id": "dev_00000000", "rest": "nothing-here"}
"""A value for each path parameter; anything else gets ``x``."""

#: The fallback that answers 404 to whatever no route took. It changes nothing.
NO_SUCH_ROUTE = "/api/{rest:path}"
#: The write dispatcher: one route, a name per write.
WRITES = "/api/{name:path}"
#: A method set for a route that takes any method.
ANY_METHOD = ("GET", "PUT", "PATCH", "DELETE")


def _sources() -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
        transcript=lambda agent, project, limit, before, width: {"lines": [], "cursor": None},
        explainability=lambda agent, project: {"available": False},
    )


def _writes(ran: list[str]) -> Writes:
    def handler(name: str) -> remote_server.WriteHandler:
        def run(body: dict[str, Any]) -> tuple[dict[str, object], str]:
            ran.append(name)
            return {"ok": True}, name

        return run

    return Writes({name: handler(name) for name in write_endpoint_names()})


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    return make_runtime()


@pytest.fixture
def ran() -> list[str]:
    return []


@pytest.fixture
def app(runtime: Runtime, ran: list[str], tmp_path: Path) -> Any:
    return build_app(runtime, sources=_sources(), writes=_writes(ran), dist_dir=tmp_path)


def _concrete(path: str, **values: str) -> str:
    return re.sub(
        r"\{(\w+)(?::\w+)?\}",
        lambda match: values.get(match.group(1)) or SAMPLE.get(match.group(1), "x"),
        path,
    )


def _calls(app: Any) -> Iterator[tuple[str, str, str]]:
    """``(method, template, concrete path)`` for every method of every ``/api`` route.

    The write dispatcher once per name it answers; the fallback once per method.
    """
    for route in mounted_routes(app):
        if not isinstance(route, Route) or not route.path.startswith("/api/"):
            continue
        methods = sorted(route.methods or ANY_METHOD)
        if route.path == WRITES:
            for name in write_endpoint_names():
                yield "POST", route.path, f"/api/{name}"
            continue
        for method in methods:
            yield method, route.path, _concrete(route.path)


def _sample_app() -> Any:
    """The app the parametrizations are computed from, at collection: its route table is
    all they read. Its runtime is never initialised, because a real one would load (and
    write) ``remote.json`` from the developer's own home before any fixture isolates it."""
    return build_app(
        Runtime.__new__(Runtime), sources=_sources(), writes=_writes([]), dist_dir=Path()
    )


def _ids(call: tuple[str, str, str]) -> str:
    return f"{call[0]} {call[2]}"


ALL_CALLS = list(_calls(_sample_app()))
BODIED = [c for c in ALL_CALLS if c[0] not in ("GET", "HEAD", "OPTIONS")]
GATED = [c for c in BODIED if (c[0], c[1]) not in NOT_WRITE_GATED and c[1] != NO_SUCH_ROUTE]


def test_the_walk_sees_the_routes_it_must() -> None:
    """The control: a walk that found nothing would pass every test below."""
    paths = {template for _method, template, _path in ALL_CALLS}
    assert {"/api/unlock", "/api/board", "/api/devices/{device_id}", WRITES} <= paths
    assert ("POST", "/api/remote/extend") in {(m, t) for m, t, _p in GATED}
    assert {f"/api/{name}" for name in write_endpoint_names()} <= {p for _m, _t, p in GATED}


# --- gate 4: a device for every /api route but unlock, and for the socket ----------------


@pytest.mark.parametrize("call", [c for c in ALL_CALLS if c[1] != "/api/unlock"], ids=_ids)
def test_every_api_route_without_a_cookie_is_401(
    app: Any, runtime: Runtime, ran: list[str], call: tuple[str, str, str]
) -> None:
    method, _template, path = call
    response = make_client(app).request(method, f"{base(runtime)}{path}", content=b"{}")
    assert response.status_code == 401, response.text
    if method != "HEAD":
        assert response.json() == {"error": "unauthorized"}
    assert ran == []


def test_a_forged_cookie_is_401_before_any_route(app: Any, runtime: Runtime) -> None:
    client = make_client(app)
    client.cookies.set(remote_server.COOKIE, "made-up")
    assert client.get(f"{base(runtime)}/api/board").status_code == 401
    assert client.post(f"{base(runtime)}/api/note", json={}).status_code == 401


def test_the_socket_without_a_cookie_is_a_401_denial(app: Any, runtime: Runtime) -> None:
    client = make_client(app)
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"{base(runtime)}/ws"),
    ):
        pass
    assert denied.value.status_code == 401


def test_without_the_denial_extension_the_socket_is_closed_4401(runtime: Runtime) -> None:
    """A server that cannot send a denial response gets the close code the page maps."""
    app = build_app(runtime, sources=_sources(), dist_dir=Path())
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "scheme": "ws",
        "path": f"{base(runtime)}/ws",
        "headers": [(b"host", b"testserver"), (b"origin", b"http://testserver")],
        "extensions": {},
    }
    asyncio.run(app(scope, receive, send))
    assert sent == [{"type": "websocket.close", "code": remote_server.WS_CLOSE_UNAUTHORIZED}]


def test_the_page_and_unlock_need_no_device(app: Any, runtime: Runtime) -> None:
    client = make_client(app)
    assert client.get(f"{base(runtime)}/").status_code != 401
    assert unlock(client, runtime).status_code == 200


# --- gate 5: no body over 64 KiB reaches a route -------------------------------------------


@pytest.mark.parametrize("call", BODIED, ids=_ids)
def test_a_declared_body_over_the_cap_is_413(
    app: Any, runtime: Runtime, ran: list[str], call: tuple[str, str, str]
) -> None:
    method, _template, path = call
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    body = b"x" * (MAX_BODY_BYTES + 1)
    response = client.request(method, f"{base(runtime)}{path}", content=body)
    assert response.status_code == 413, response.text
    assert response.json()["error"] == "too_large"
    assert ran == []


@pytest.mark.parametrize("call", BODIED, ids=_ids)
def test_a_chunked_body_over_the_cap_is_413(
    app: Any, runtime: Runtime, ran: list[str], call: tuple[str, str, str]
) -> None:
    method, _template, path = call
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    chunks = iter([b"x" * MAX_BODY_BYTES, b"x"])  # no Content-Length: chunked
    response = client.request(method, f"{base(runtime)}{path}", content=chunks)
    assert response.status_code == 413, response.text
    assert ran == []


def test_unlock_is_capped_too_it_is_the_body_a_stranger_can_send(
    app: Any, runtime: Runtime
) -> None:
    oversized = {"password": "x" * MAX_BODY_BYTES}
    response = make_client(app).post(f"{base(runtime)}/api/unlock", json=oversized)
    assert response.status_code == 413


def test_a_body_at_the_cap_reaches_its_route(app: Any, runtime: Runtime, ran: list[str]) -> None:
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    padded = {"text": ""}
    padded["text"] = "x" * (MAX_BODY_BYTES - len(json.dumps(padded).encode()))
    body = json.dumps(padded).encode()
    assert len(body) == MAX_BODY_BYTES
    response = client.post(f"{base(runtime)}/api/note", content=body)
    assert response.status_code == 200, response.text
    assert ran == ["note"]


def _receiver(*messages: dict[str, Any]) -> tuple[Any, list[int]]:
    """A ``receive`` that hands out ``messages`` in order and counts how many it gave."""
    given: list[int] = [0]

    async def receive() -> dict[str, Any]:
        given[0] += 1
        if given[0] > len(messages):
            return {"type": "http.disconnect"}
        return messages[given[0] - 1]

    return receive, given


def test_a_declared_length_is_refused_before_a_byte_is_read() -> None:
    receive, given = _receiver({"type": "http.request", "body": b"{}"})
    scope = {"headers": [(b"content-length", str(MAX_BODY_BYTES + 1).encode())]}
    assert asyncio.run(remote_gate_body(scope, receive)) is None
    assert given == [0], "the cap was checked against the header, nothing was received"


def test_a_chunked_read_stops_at_the_cap_plus_one() -> None:
    half = b"x" * (MAX_BODY_BYTES // 2 + 1)
    receive, given = _receiver(
        {"type": "http.request", "body": half, "more_body": True},
        {"type": "http.request", "body": half, "more_body": True},
        {"type": "http.request", "body": b"never read", "more_body": False},
    )
    assert asyncio.run(remote_gate_body({"headers": []}, receive)) is None
    assert given == [2], "the read ended at the chunk that passed the cap"


def test_a_body_in_chunks_is_replayed_as_one_message_then_the_server_answers() -> None:
    receive, _given = _receiver(
        {"type": "http.request", "body": b'{"text":', "more_body": True},
        {"type": "http.request", "body": b' "hi"}', "more_body": False},
    )

    async def drain() -> list[dict[str, Any]]:
        replayed = await remote_gate_body({"headers": []}, receive)
        assert replayed is not None
        return [await replayed(), await replayed()]

    first, after = asyncio.run(drain())
    assert first == {"type": "http.request", "body": b'{"text": "hi"}', "more_body": False}
    assert after == {"type": "http.disconnect"}, "after the body, the server's own receive"


# --- the write gate: every route that changes something, but the frozen few ----------------


@pytest.mark.parametrize("call", GATED, ids=_ids)
def test_every_write_is_403_until_writes_are_on(
    app: Any, runtime: Runtime, ran: list[str], call: tuple[str, str, str]
) -> None:
    """The body is ``{}``: the write gate runs before any validation could answer first."""
    method, _template, path = call
    client = make_client(app)
    unlock(client, runtime)
    assert runtime.allow_write is False
    response = client.request(method, f"{base(runtime)}{path}", json={})
    assert response.status_code == 403, response.text
    assert response.json() == {"error": "read_only", "message": READ_ONLY_REASON}
    assert ran == []


def test_signing_out_needs_no_write_but_revoking_another_device_does(
    app: Any, runtime: Runtime
) -> None:
    mine, theirs = make_client(app), make_client(app)
    unlock(mine, runtime)
    unlock(theirs, runtime)
    rows = mine.get(f"{base(runtime)}/api/devices").json()
    own = next(row["id"] for row in rows if row["current"])
    other = next(row["id"] for row in rows if not row["current"])
    refused = mine.delete(f"{base(runtime)}/api/devices/{other}")
    assert refused.status_code == 403 and refused.json()["error"] == "read_only"
    assert mine.delete(f"{base(runtime)}/api/devices/{own}").status_code == 200
    assert mine.get(f"{base(runtime)}/api/board").status_code == 401, "signed out"
    assert theirs.get(f"{base(runtime)}/api/board").status_code == 200, "and nobody else"


def test_not_write_gated_is_the_frozen_list() -> None:
    assert (
        frozenset(
            {
                ("POST", "/api/unlock"),
                ("DELETE", "/api/devices/{device_id}"),
                ("POST", "/api/needs/dismiss"),
                ("POST", "/api/push/subscribe"),
                ("DELETE", "/api/push/subscription"),
                ("POST", "/api/push/test"),
            }
        )
        == NOT_WRITE_GATED
    )


# --- one body parser ---------------------------------------------------------------------

_BODY_READERS = {"json", "body", "stream", "form"}


def _body_reads(source: str) -> list[int]:
    """Lines that call ``.json()``/``.body()``/``.stream()``/``.form()`` outside
    ``kit_json_object``, the one parser."""
    tree = ast.parse(source)
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            node.name == "kit_json_object"
        ):
            allowed |= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _BODY_READERS
        and node.lineno not in allowed
    ]


def test_no_remote_module_reads_a_body_outside_kit_json_object() -> None:
    modules = sorted(SERVICES.glob("remote_*.py"))
    assert len(modules) >= 4, modules  # server, needs, push, actions at least
    offenders = {
        module.name: lines
        for module in modules
        if (lines := _body_reads(module.read_text(encoding="utf-8")))
    }
    assert not offenders, f"read the body through RemoteKit.kit_json_object: {offenders}"


def test_the_body_read_check_can_fail() -> None:
    """The control: the same check flags a handler that parses its own body."""
    source = (
        "async def handler(request):\n"
        "    return await request.json()\n\n"
        "async def kit_json_object(self, request):\n"
        "    return await request.body()\n"
    )
    assert _body_reads(source) == [2]


def test_a_body_that_is_not_an_object_is_400_through_the_one_parser(
    app: Any, runtime: Runtime
) -> None:
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    for body in (b"[1, 2]", b"not json", b'"a string"'):
        response = client.post(f"{base(runtime)}/api/note", content=body)
        assert response.status_code == 400, body
        assert response.json()["error"] == "invalid"
    assert client.post(f"{base(runtime)}/api/note", content=b"").status_code == 200, (
        "an empty body is an empty object"
    )
