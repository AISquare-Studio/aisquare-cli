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
        discovery_overrides: dict[str, Any] | None = None,
        verification_uri: str | None = None,
    ) -> None:
        self.token_script = list(token_script or ["pending", "token"])
        self.discovery = discovery
        self.discovery_overrides = dict(discovery_overrides or {})
        self.verification_uri = verification_uri
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
        # The API behind the session (#142): what a signed-in caller can list and do.
        # ``workspaces`` rows follow /api/v2/workspaces/; ``studios`` maps a workspace
        # id (as a string) to its /api/v2/publications/ rows; ``credits`` is the
        # /api/v2/credits/balance/ payload for whichever workspace the header names.
        self.workspaces: list[dict[str, Any]] = []
        self.studios: dict[str, list[dict[str, Any]]] = {}
        self.credits: dict[str, Any] | None = None
        self.page_size = 50
        #: "ok" mints keys; "token_not_valid" is what the API says TODAY to a sign-in
        #: token on the key endpoints (their authentication class predates OAuth).
        self.key_mint = "ok"
        self.minted: list[dict[str, Any]] = []
        self.revoked_keys: list[str] = []
        #: Workspace keys the API knows besides the minted ones (a key attached by hand).
        self.accepted_keys: list[str] = []
        #: Agent-name → studio bindings written through the routing endpoint, per workspace.
        self.bindings: dict[str, dict[str, int]] = {}
        #: "ok" binds; "forbidden" is what a key whose owner is neither OWNER/ADMIN
        #: nor the studio's owner gets (403, per name).
        self.routing = "ok"
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:  # silence the test log
                return

            def _record(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                content_type = self.headers.get("Content-Type") or ""
                body = json.loads(raw) if raw and "json" in content_type else None
                parsed = urlparse(self.path)
                record = {
                    "method": self.command,
                    "path": parsed.path,
                    "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "form": form,
                    "json": body,
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

            def do_PUT(self) -> None:
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
            document = {
                "issuer": f"{self.url}/o",
                "device_authorization_endpoint": f"{self.url}/o/device-authorization/",
                "token_endpoint": f"{self.url}/o/token/",
                "userinfo_endpoint": f"{self.url}/o/userinfo/",
                "revocation_endpoint": f"{self.url}/o/revoke_token/",
                "jwks_uri": f"{self.url}/o/.well-known/jwks.json",
                "grant_types_supported": ["urn:ietf:params:oauth:grant-type:device_code"],
                "scopes_supported": ["openid", "profile", "email", "aisquare"],
            }
            document.update(self.discovery_overrides)
            handler._send(200, document)
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
                    "verification_uri": self.verification_uri or f"{self.url}/cli",
                    "verification_uri_complete": (
                        f"{self.verification_uri or f'{self.url}/cli'}?code={USER_CODE}"
                    ),
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
        if path.startswith("/api/v2/"):
            self._api(handler, record, path)
            return
        handler._send(404, {"detail": "no route"})

    # ---- the API behind the session (#142) -----------------------------------

    def _signed_in(self, record: dict[str, Any]) -> bool:
        bearer = record["headers"].get("authorization", "").removeprefix("Bearer ").strip()
        return bearer in self.issued and bearer not in self.revoked

    def _page(self, record: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        """The API's envelope: ``count/next/previous/page/page_size/total/results``."""
        page = int(record["query"].get("page", "1"))
        # ``page_size`` is the server's MAXIMUM, as the real API's is (100): a
        # client may ask for less, never for more — which is how a test makes a
        # three-row listing span two pages.
        size = min(int(record["query"].get("page_size", self.page_size)), self.page_size)
        start = (page - 1) * size
        chunk = rows[start : start + size]
        more = start + size < len(rows)
        here = f"{self.url}{record['path']}"
        return {
            "count": len(rows),
            "next": f"{here}?page={page + 1}&page_size={size}" if more else None,
            "previous": None if page == 1 else f"{here}?page={page - 1}",
            "page": page,
            "page_size": size,
            "total": len(rows),
            "results": chunk,
        }

    def _workspace_rows(self, record: dict[str, Any]) -> list[dict[str, Any]] | None:
        """The studios of the workspace ``X-Workspace-Id`` names (id or uid); ``None`` if none."""
        header = record["headers"].get("x-workspace-id")
        if not header:
            return None
        for workspace in self.workspaces:
            if header in (str(workspace.get("id")), str(workspace.get("uid"))):
                return self.studios.get(str(workspace["id"]), [])
        return None

    def _api_key_ok(self, record: dict[str, Any]) -> bool:
        key = record["headers"].get("x-api-key", "")
        return bool(key) and (
            key in self.accepted_keys or any(k["api_key"] == key for k in self.minted)
        )

    def _api(self, handler: Any, record: dict[str, Any], path: str) -> None:
        # The routing endpoints take an API KEY (the gateway calls them with the
        # ingest key); like the real ones they do not take a sign-in token.
        if "/agents/" in path and path.startswith("/api/v2/iam/workspaces/"):
            if not self._api_key_ok(record):
                handler._send(401, {"detail": "Given token not valid", "code": "token_not_valid"})
                return
            # /api/v2/iam/workspaces/<id>/agents/[<name>/]
            segments = [s for s in path.split("/") if s]
            workspace_id = segments[4]
            if record["method"] == "PUT" and len(segments) == 7:
                agent = segments[6]
                if self.routing == "forbidden":
                    handler._send(403, {"detail": "You must be a workspace OWNER/ADMIN …"})
                    return
                body = record["json"] or {}
                studio = int(body.get("publication_id", 0))
                known = {s["id"] for s in self.studios.get(workspace_id, [])}
                if studio not in known:
                    handler._send(404, {"detail": "Publication not found."})
                    return
                self.bindings.setdefault(workspace_id, {})[agent] = studio
                handler._send(
                    200,
                    {
                        "name": agent,
                        "workspace_id": int(workspace_id),
                        "publication_id": studio,
                        "publication_name": next(
                            s["name"] for s in self.studios[workspace_id] if s["id"] == studio
                        ),
                    },
                )
                return
            if record["method"] == "GET" and len(segments) == 6:
                rows = [
                    {"name": agent, "workspace_id": int(workspace_id), "publication_id": studio}
                    for agent, studio in self.bindings.get(workspace_id, {}).items()
                ]
                handler._send(200, rows)
                return
            handler._send(404, {"detail": "no route"})
            return
        if not self._signed_in(record):
            handler._send(
                401, {"detail": "Authentication credentials were not provided.", "code": "na"}
            )
            return
        if path == "/api/v2/workspaces/":
            needle = record["query"].get("q", "").lower()
            rows = [w for w in self.workspaces if needle in str(w.get("name", "")).lower()]
            handler._send(200, self._page(record, rows))
            return
        if path == "/api/v2/publications/":
            # The real API falls back to the caller's personal workspace when the
            # header names nothing it knows, rather than failing; the stub has no
            # personal workspace, so a wrong header buys an empty page.
            studios = self._workspace_rows(record) or []
            handler._send(200, self._page(record, studios))
            return
        if path == "/api/v2/credits/balance/":
            if self._workspace_rows(record) is None:
                handler._send(
                    400, {"error": "Workspace context required. Please select a workspace."}
                )
            elif self.credits is None:
                handler._send(404, {"detail": "Not found."})
            else:
                handler._send(200, self.credits)
            return
        if path == "/api/v2/iam/workspace-api-key/" and record["method"] == "POST":
            if self.key_mint == "token_not_valid":
                handler._send(401, {"detail": "Given token not valid", "code": "token_not_valid"})
                return
            body = record["json"] or {}
            key = {
                "uid": f"key-{len(self.minted) + 1}",
                "name": body.get("name", ""),
                "api_key": f"AIS_minted{len(self.minted) + 1}_{'x' * 20}",
                "workspace_id": body.get("workspace_id"),
                "scopes": body.get("scopes", ["*"]),
                "is_active": True,
                "last_used_at": None,
                "created_at": "2026-09-13T10:00:00Z",
            }
            self.minted.append(key)
            handler._send(201, key)
            return
        if path.startswith("/api/v2/iam/workspace-api-key/") and path.endswith("/revoke/"):
            if self.key_mint == "token_not_valid":
                handler._send(401, {"detail": "Given token not valid", "code": "token_not_valid"})
                return
            self.revoked_keys.append(path.split("/")[-3])
            handler._send(204)
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
