"""A loopback identity provider that speaks exactly the contract the CLI follows.

Discovery, RFC 8628 device authorization, a scripted token endpoint, userinfo
and RFC 7009 revocation, in the shapes ``docs/plans/aisquare-login.md``
freezes. The token endpoint answers from ``token_script`` one entry per poll
(the last entry repeats), so a test states the server's behaviour as data:
``["pending", "slow_down", "token"]``.

Same pattern as ``tests/test_explainability_ops._gateway``: a real HTTP
server on an ephemeral port, so the code under test exercises its actual
``urllib`` path rather than a mock of it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

USER_CODE = "WDJB-MJHT"
DEVICE_CODE = "dev-code-" + "x" * 21
DISCOVERY_PATH = "/o/.well-known/openid-configuration"


class IdentityProviderStub:
    def __init__(
        self,
        token_script: list[str] | None = None,
        *,
        discovery: bool = True,
        interval: int = 0,
        expires_in: int = 900,
        start_status: int = 200,
    ) -> None:
        self.token_script = list(token_script or ["pending", "token"])
        self.discovery = discovery
        self.interval = interval
        self.expires_in = expires_in
        self.start_status = start_status
        self.requests: list[dict[str, Any]] = []
        self.issued: list[str] = []
        self.revoked: list[str] = []
        self.claims: dict[str, Any] = {
            "sub": "uid-123",
            "email": "anmol@example.com",
            "email_verified": True,
            "name": "Anmol Majithia",
            "preferred_username": "anmol",
        }
        self.token_lifetime = 90 * 24 * 3600
        self.retry_after = 1
        self._polls = 0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:  # silence the test log
                return

            def _record(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                record = {
                    "method": self.command,
                    "path": urlparse(self.path).path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "form": form,
                }
                stub.requests.append(record)
                return record

            def _send(
                self, status: int, body: Any = None, headers: dict[str, str] | None = None
            ) -> None:
                payload = json.dumps(body).encode("utf-8") if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                record = self._record()
                stub.route(self, record)

            def do_POST(self) -> None:
                record = self._record()
                stub.route(self, record)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    # ---- behaviour ----------------------------------------------------------

    def route(self, handler: Any, record: dict[str, Any]) -> None:
        path = record["path"]
        if path == DISCOVERY_PATH:
            if not self.discovery:
                handler._send(404, {"detail": "Not found."})
                return
            handler._send(
                200,
                {
                    "issuer": f"{self.url}/o",
                    "device_authorization_endpoint": f"{self.url}/o/device-authorization/",
                    "token_endpoint": f"{self.url}/o/token/",
                    "userinfo_endpoint": f"{self.url}/o/userinfo/",
                    "revocation_endpoint": f"{self.url}/o/revoke_token/",
                    "jwks_uri": f"{self.url}/o/.well-known/jwks.json",
                    "grant_types_supported": ["urn:ietf:params:oauth:grant-type:device_code"],
                    "scopes_supported": ["openid", "profile", "email", "aisquare"],
                },
            )
            return
        if path == "/o/device-authorization/":
            if self.start_status == 429:
                handler._send(
                    429, {"error": "rate_limited"}, {"Retry-After": str(self.retry_after)}
                )
                return
            if self.start_status != 200:
                handler._send(self.start_status, {"error": "temporarily_unavailable"})
                return
            handler._send(
                200,
                {
                    "device_code": DEVICE_CODE,
                    "user_code": USER_CODE,
                    "verification_uri": f"{self.url}/cli",
                    "verification_uri_complete": f"{self.url}/cli?code={USER_CODE}",
                    "expires_in": self.expires_in,
                    "interval": self.interval,
                },
            )
            return
        if path == "/o/token/":
            self._token(handler, record)
            return
        if path == "/o/userinfo/":
            bearer = record["headers"].get("authorization", "").removeprefix("Bearer ").strip()
            if bearer in self.issued and bearer not in self.revoked:
                handler._send(200, self.claims)
            else:
                handler._send(401, {"error": "invalid_token"})
            return
        if path == "/o/revoke_token/":
            self.revoked.append(record["form"].get("token", ""))
            handler._send(200)
            return
        if path == "/api/v1/ping/":
            bearer = record["headers"].get("authorization", "").removeprefix("Bearer ").strip()
            if bearer in self.issued and bearer not in self.revoked:
                handler._send(
                    200, {"pong": True, "workspace": record["headers"].get("x-workspace-id")}
                )
            else:
                handler._send(401, {"detail": "Given token not valid", "code": "token_not_valid"})
            return
        handler._send(404, {"detail": "no route"})

    def _token(self, handler: Any, record: dict[str, Any]) -> None:
        if record["form"].get("device_code") != DEVICE_CODE:
            handler._send(400, {"error": "invalid_grant"})
            return
        step = self.token_script[min(self._polls, len(self.token_script) - 1)]
        self._polls += 1
        if step == "pending":
            handler._send(400, {"error": "authorization_pending"})
        elif step == "slow_down":
            handler._send(400, {"error": "slow_down", "interval": 10})
        elif step == "denied":
            handler._send(400, {"error": "access_denied"})
        elif step == "expired":
            handler._send(400, {"error": "expired_token"})
        elif step == "rate_limited":
            handler._send(429, {"error": "rate_limited"}, {"Retry-After": str(self.retry_after)})
        elif step == "paused":
            handler._send(503, {"error": "temporarily_unavailable"})
        elif step == "token":
            token = f"aisq_{len(self.issued):02d}" + "t" * 41
            self.issued.append(token)
            handler._send(
                200,
                {
                    "access_token": token,
                    "token_type": "Bearer",
                    "expires_in": self.token_lifetime,
                    "scope": "openid profile email aisquare",
                },
            )
        else:  # pragma: no cover - a typo in a test script
            handler._send(500, {"error": f"unknown script step {step}"})

    # ---- introspection for assertions ---------------------------------------

    def paths(self) -> list[str]:
        return [request["path"] for request in self.requests]

    def polls(self) -> int:
        return self._polls

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
