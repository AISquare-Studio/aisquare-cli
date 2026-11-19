"""The AISquare identity provider, spoken with the standard library.

This is the ONE module that talks to ``/o/`` and the ONE reader of the
``iam_*`` credential keys (``tests/test_iam_single_reader.py`` pins both).
Everything else asks it for a session or a token and never touches the file.

It is a standards client, not an AISquare-specific one: it reads the OpenID
discovery document and follows the endpoints it advertises, signs in with the
RFC 8628 device grant, identifies the user through userinfo and signs out
through RFC 7009 revocation. The contract it follows is
``docs/plans/aisquare-login.md``.

Every network import lives inside a function. ``cli/root.py`` imports the
auth service at module scope, and the CLI's import-cost ratchet
(``tests/test_import_cost_of_the_integration.py``) asserts that ``ssl`` and
``http`` are not in the base import closure.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from aisquare.core import credentials, paths
from aisquare.core import program as prog
from aisquare.core.config import load_config
from aisquare.core.version import __version__

CLIENT_ID = "rlx"
"""The client a sign-in starts as: the server sends the person to its own ``/rlx`` page."""
FALLBACK_CLIENT_ID = "aisquare-cli"
"""The client a server that does not know ``rlx`` yet still serves, at its ``/cli`` page.
Also the client of every session stored before 0.10.1: 0.7.0 to 0.10.0 recorded it."""
UNKNOWN_CLIENT_STATUSES = frozenset({400, 401})
"""The statuses with which an ``invalid_client`` start means "this server does not know rlx":
401, as production and staging answer, and 400, which RFC 6749 allows where no client
authentication is used. Either falls back to :data:`FALLBACK_CLIENT_ID`."""
SCOPE = "openid profile email aisquare"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

TOKEN_ENV_VAR = "AISQUARE_TOKEN"
API_URL_ENV_VAR = "AISQUARE_API_URL"

HTTP_TIMEOUT_SECONDS = 10.0
DISCOVERY_TTL_SECONDS = 24 * 3600
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# The credential keys, all strings, merged into ~/.aisquare/credentials by
# core.credentials.store. Read here and nowhere else.
KEY_API_URL = "iam_api_url"
KEY_TOKEN = "iam_token"
KEY_EXPIRES_AT = "iam_token_expires_at"
KEY_SCOPE = "iam_scope"
KEY_SUB = "iam_sub"
KEY_EMAIL = "iam_email"
KEY_NAME = "iam_name"
KEY_CLIENT_ID = "iam_client_id"
CREDENTIAL_KEYS = (
    KEY_API_URL,
    KEY_TOKEN,
    KEY_EXPIRES_AT,
    KEY_SCOPE,
    KEY_SUB,
    KEY_EMAIL,
    KEY_NAME,
    KEY_CLIENT_ID,
)


class IamError(Exception):
    """A failure with a stable machine-readable code and a sentence for a person."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.retry_after = retry_after


@dataclass(frozen=True)
class HttpResult:
    """One HTTP exchange. Error statuses are results, not exceptions."""

    status: int
    body: Any
    headers: dict[str, str]

    def retry_after(self, default: int = 30) -> int:
        said = self.said_retry_after()
        return default if said is None else said

    def said_retry_after(self) -> int | None:
        """The ``Retry-After`` the server sent, in whole seconds (at least one), or ``None``
        where it sent none, or none in seconds."""
        raw = self.headers.get("retry-after") or self.headers.get("Retry-After") or ""
        try:
            return max(1, int(raw))
        except ValueError:
            return None

    @property
    def error(self) -> str | None:
        if isinstance(self.body, dict):
            value = self.body.get("error")
            return str(value) if value is not None else None
        return None


@dataclass(frozen=True)
class Endpoints:
    """What discovery told us. URLs are absolute."""

    issuer: str
    device_authorization: str
    token: str
    userinfo: str
    revocation: str


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    client_id: str
    """The client that started the grant. The server binds the device code to it, so every
    poll is made as this client, and the session it ends in is stored as this client's."""


@dataclass(frozen=True)
class PollOutcome:
    """One poll of the token endpoint, classified for the caller's loop."""

    kind: str  # token | pending | slow_down | denied | expired | rate_limited | paused
    token: dict[str, Any] | None = None
    interval: int | None = None
    retry_after: int | None = None


@dataclass(frozen=True)
class Session:
    """The stored (or environment-provided) sign-in."""

    api_url: str
    token: str
    source: str  # file | env
    expires_at: datetime | None = None
    scope: str = ""
    sub: str = ""
    email: str = ""
    name: str = ""
    unrestricted: bool = False
    """True on the session ``store_session`` returns when the credentials file could NOT be
    restricted to this account. A surface that cannot see stderr (the fleet UI, where Textual
    captures it) says so from this flag. A session read back from disk leaves it False."""
    client_id: str = FALLBACK_CLIENT_ID
    """The client the token was issued to, and the one that revokes it. A stored session
    that does not say is ``aisquare-cli``'s, as every session before 0.10.1 was.
    ``AISQUARE_TOKEN``'s does not say either, and nothing here revokes it."""

    def expires_in_days(self, now: datetime | None = None) -> int | None:
        if self.expires_at is None:
            return None
        current = now or datetime.now(UTC)
        return max(0, (self.expires_at - current).days)

    def as_json(self) -> dict[str, Any]:
        return {
            "user": {"sub": self.sub, "email": self.email, "name": self.name},
            "api_url": self.api_url,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "source": self.source,
        }


# --------------------------------------------------------------------------- URLs


def resolve_api_url(explicit: str | None = None) -> str:
    """``--api-url``, then ``AISQUARE_API_URL``, then ``config.toml``. Validated."""
    raw = explicit or os.environ.get(API_URL_ENV_VAR) or load_config().api_url
    url = raw.strip().rstrip("/")
    parsed = urlparse(url)
    loopback = parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS
    if parsed.scheme != "https" and not loopback:
        raise IamError(
            "api_url_not_https",
            f"Refusing to send credentials over plain http to {url}. "
            "Use https, or localhost for development.",
        )
    if not parsed.hostname:
        raise IamError("api_url_not_https", f"{url} is not a URL.")
    return url


def user_agent() -> str:
    import platform

    return f"aisquare-cli/{__version__} ({platform.system()}; {platform.machine()})"


def device_name() -> str:
    import socket

    return socket.gethostname()[:63]


# --------------------------------------------------------------------------- HTTP


def _http(
    method: str,
    url: str,
    *,
    form: dict[str, str] | None = None,
    json_body: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    follow_redirects: bool = True,
) -> HttpResult:
    """One request. HTTP error statuses come back as results; transport errors raise.

    ``follow_redirects=False`` is for a request that carries a bearer token to an endpoint
    that must answer where it is asked (:func:`post_json`): a redirect is then a result
    (``status`` 3xx), never followed, so the token cannot be sent on to another origin.

    The provider's own endpoints take form bodies (RFC 8628); the API behind the
    session takes JSON — ``json_body`` is for those (#142), and the two are
    exclusive by construction: a call names one shape.

    A transport error is ``IamError("unreachable")`` whatever layer raised it.
    ``http.client`` raises its own ``HTTPException`` subclasses, which are not
    ``OSError``s: ``IncompleteRead`` for a body shorter than its Content-Length,
    ``LineTooLong`` for a header past its limit. They escaped this function, and
    every caller that tolerates an unreachable server catches ``IamError``
    alone: one truncated answer to ``logout``'s revoke of a minted key ended
    the loop over them, and the keys after it stayed on disk after the sign-out
    (review of the accounts stack's fold, round 1, F2). An error STATUS whose
    body is cut short keeps its status, with what arrived of the body — the
    status is the answer, the body its detail — as the explainability root
    post reads one (rc/fixes' ccf4ac8).

    A success status whose body will not read is still ``unreachable``, so every
    caller that tolerates one is unchanged, but it is not SAID as one: the server
    answered, and may have done what was asked — a minted key exists whose uid
    never arrived, which nothing here can revoke — so "Could not reach" was
    wrong, and invited a retry that mints another (review of the accounts
    stack's fold, round 2, F3).
    """
    from http.client import HTTPException, IncompleteRead
    from urllib.error import HTTPError, URLError
    from urllib.request import Request

    from aisquare.core import tls

    def urlopen(request: Any, *, timeout: float) -> Any:
        if follow_redirects:
            return tls.urlopen(request, timeout=timeout)
        return tls.urlopen_without_redirects(request, timeout=timeout)

    if form is not None and json_body is not None:
        raise ValueError("a request has one body shape: form or JSON, not both")
    data = urlencode(form).encode("utf-8") if form is not None else None
    request_headers = {"Accept": "application/json", "User-Agent": user_agent()}
    if data is not None:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            try:
                raw = response.read()
            except (HTTPException, OSError) as exc:
                landed = "" if method == "GET" else "; the request may have been carried out"
                raise IamError(
                    "unreachable",
                    f"{url} answered HTTP {response.status}, but the answer was cut short "
                    f"({exc!r}){landed}.",
                    detail=str(exc),
                ) from exc
            return HttpResult(response.status, _parse(raw), dict(response.headers))
    except HTTPError as exc:
        # Read here, inside the handler, where the clause below cannot catch it.
        try:
            raw = exc.read()
        except IncompleteRead as short:
            raw = short.partial
        except (HTTPException, OSError):
            raw = b""
        return HttpResult(exc.code, _parse(raw), dict(exc.headers))
    except (URLError, OSError, TimeoutError, ValueError, HTTPException) as exc:
        raise IamError("unreachable", f"Could not reach {url}: {exc}.", detail=str(exc)) from exc


def _parse(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


# --------------------------------------------------------------------------- discovery


def _discovery_cache_path(api_url: str) -> Path:
    parsed = urlparse(api_url)
    host = (parsed.hostname or "unknown").replace(":", "_")
    port = f"-{parsed.port}" if parsed.port else ""
    return paths.oidc_cache_dir() / f"{host}{port}.json"


def discover(api_url: str, *, refresh: bool = False) -> Endpoints:
    """The endpoints ``{api_url}/o/.well-known/openid-configuration`` advertises, cached a day."""
    cache_file = _discovery_cache_path(api_url)
    if not refresh and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("fetched_at", 0)) < DISCOVERY_TTL_SECONDS:
                return _endpoints_from(cached["document"], api_url)
        except (OSError, ValueError, KeyError, TypeError, IamError):
            pass  # a damaged cache is refetched, never trusted

    result = _http("GET", f"{api_url}/o/.well-known/openid-configuration")
    if result.status != 200 or not isinstance(result.body, dict):
        raise IamError(
            "unsupported_server",
            f"{api_url} does not offer device sign-in (no OpenID discovery document).",
            detail=f"HTTP {result.status}",
        )
    endpoints = _endpoints_from(result.body, api_url)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"fetched_at": time.time(), "document": result.body}), encoding="utf-8"
        )
        # The document names where the token goes; nobody else on the machine
        # gets to edit that. (Every URL in it is also re-checked on read.)
        cache_file.chmod(0o600)
    except OSError:
        pass  # the cache is a convenience
    return endpoints


def _endpoints_from(document: dict[str, Any], api_url: str) -> Endpoints:
    """Read the discovery document, refusing any endpoint off the API's origin.

    ``resolve_api_url`` only lets https (or loopback http) through the front
    door; a document, from the server or from the on-disk cache, must not be
    able to point the device code and then the bearer somewhere else.
    """
    required = ("device_authorization_endpoint", "token_endpoint", "userinfo_endpoint")
    missing = [key for key in required if not document.get(key)]
    if missing:
        raise IamError(
            "unsupported_server",
            f"{api_url} does not offer device sign-in (discovery lacks {', '.join(missing)}).",
        )
    revocation = document.get("revocation_endpoint") or f"{api_url}/o/revoke_token/"
    return Endpoints(
        issuer=str(document.get("issuer", api_url)),
        device_authorization=_same_origin(document["device_authorization_endpoint"], api_url),
        token=_same_origin(document["token_endpoint"], api_url),
        userinfo=_same_origin(document["userinfo_endpoint"], api_url),
        revocation=_same_origin(revocation, api_url),
    )


def _safe_scheme(url: str) -> bool:
    parsed = urlparse(url)
    loopback = parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS
    return bool(parsed.hostname) and (parsed.scheme == "https" or loopback)


def _same_origin(value: Any, api_url: str) -> str:
    """An endpoint credentials will be sent to: https (or loopback) AND the API's own origin."""
    url = str(value)
    parsed, api = urlparse(url), urlparse(api_url)
    if not _safe_scheme(url) or (parsed.scheme, parsed.netloc.lower()) != (
        api.scheme,
        api.netloc.lower(),
    ):
        raise IamError(
            "unsupported_server",
            f"{api_url} advertises an endpoint on another server ({url}). "
            "Refusing to send credentials there.",
        )
    return url


def _safe_link(value: Any, issuer: str) -> str:
    """A link the person will open: any host, but never plain http off the loopback."""
    url = str(value)
    if not _safe_scheme(url):
        raise IamError(
            "unsupported_server", f"{issuer} sent a sign-in link over plain http ({url}). Refusing."
        )
    return url


# --------------------------------------------------------------------------- device flow


def _in_minutes(seconds: int) -> str:
    """A wait as a person reads it: whole minutes, rounded up, at least one."""
    minutes = max(1, -(-seconds // 60))
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


def _reject_common(result: HttpResult, what: str) -> None:
    """The answers a server may give before it looks at the request: its per-IP rate limit
    (429 ``too_many_requests`` with ``Retry-After``) and its kill switch (503
    ``temporarily_unavailable``), and a server error. Each is said with when to try again, the
    server's ``Retry-After`` where it sent one: a sign-in is the person's to start again, and a
    wait of minutes is not one to sit through in silence (R26a)."""
    if result.status == 429:
        wait = result.retry_after()
        raise IamError(
            "rate_limited",
            f"Too many sign-in attempts from this network. Try again in {_in_minutes(wait)}.",
            retry_after=wait,
        )
    if result.status == 503:
        if result.said_retry_after() is None:
            raise IamError("paused", "Sign-in from rlx is temporarily paused. Try again later.")
        wait = result.retry_after()
        raise IamError(
            "paused",
            f"Sign-in from rlx is temporarily paused. Try again in {_in_minutes(wait)}.",
            retry_after=wait,
        )
    if result.status >= 500:
        raise IamError("unreachable", f"The server could not {what} (HTTP {result.status}).")


def start_device_authorization(endpoints: Endpoints) -> DeviceAuthorization:
    """Start the device grant as ``rlx``, or as ``aisquare-cli`` on a server that does not know rlx.

    Such a server answers ``invalid_client``, and the grant starts again as ``aisquare-cli``,
    whose ``/cli`` page signs the person in just the same: a normal fallback, so nothing is
    said about it. Production and staging answer HTTP 401, ``{"error": "invalid_client",
    "error_description": "Unknown client_id."}`` (cedar-swan, live, 2026-11-18); a 400 with
    it falls back too, which RFC 6749 allows where no client authentication is used (R26a,
    the final review of #503, M2). The error is read from the parsed JSON body. Any other
    answer is the server's answer, said as it always was, and never a reason to try the other
    client: ``unauthorized_client``, and the two a server gives before it checks the client,
    its rate limit (429) and its kill switch (503), said with when to try again
    (:func:`_reject_common`).

    The links come from the response, never from here: the page lives on the web app's
    host, which differs per environment and is not the API's.
    """
    client_id = CLIENT_ID
    result = _start_as(endpoints, client_id)
    if result.status in UNKNOWN_CLIENT_STATUSES and result.error == "invalid_client":
        client_id = FALLBACK_CLIENT_ID
        result = _start_as(endpoints, client_id)
    _reject_common(result, "start a sign-in")
    if result.status != 200 or not isinstance(result.body, dict):
        raise IamError(
            "unsupported_server",
            f"{endpoints.issuer} refused to start a device sign-in "
            f"({result.error or 'HTTP ' + str(result.status)}).",
        )
    body = result.body
    try:
        return DeviceAuthorization(
            device_code=str(body["device_code"]),
            user_code=str(body["user_code"]),
            verification_uri=_safe_link(body["verification_uri"], endpoints.issuer),
            verification_uri_complete=_safe_link(
                body.get("verification_uri_complete") or body["verification_uri"], endpoints.issuer
            ),
            expires_in=int(body.get("expires_in", 900)),
            interval=int(body.get("interval", 5)),
            client_id=client_id,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise IamError(
            "unsupported_server", f"{endpoints.issuer} returned an incomplete device response."
        ) from exc


def _start_as(endpoints: Endpoints, client_id: str) -> HttpResult:
    return _http(
        "POST",
        endpoints.device_authorization,
        form={"client_id": client_id, "scope": SCOPE},
        headers={"X-Device-Name": device_name()},
    )


def poll_token(endpoints: Endpoints, grant: DeviceAuthorization) -> PollOutcome:
    """One poll for ``grant``'s token, as the client that started it.

    The server binds a device code to its client, and answers another client's poll for it
    with ``invalid_grant``, which reads here as an expired code.
    """
    result = _http(
        "POST",
        endpoints.token,
        form={
            "grant_type": DEVICE_GRANT,
            "device_code": grant.device_code,
            "client_id": grant.client_id,
        },
    )
    if result.status == 200 and isinstance(result.body, dict) and result.body.get("access_token"):
        return PollOutcome("token", token=result.body)
    if result.status == 429:
        return PollOutcome("rate_limited", retry_after=result.retry_after())
    if result.status == 503:
        return PollOutcome("paused")
    error = result.error or ""
    if error == "authorization_pending":
        return PollOutcome("pending")
    if error == "slow_down":
        interval = result.body.get("interval") if isinstance(result.body, dict) else None
        return PollOutcome("slow_down", interval=int(interval) if interval else None)
    if error == "access_denied":
        return PollOutcome("denied")
    if error in ("expired_token", "invalid_grant"):
        return PollOutcome("expired")
    raise IamError(
        "unsupported_server",
        f"{endpoints.issuer} answered the token request with "
        f"{error or 'HTTP ' + str(result.status)}.",
    )


def fetch_userinfo(endpoints: Endpoints, token: str) -> dict[str, Any]:
    result = _http("GET", endpoints.userinfo, headers={"Authorization": f"Bearer {token}"})
    if result.status == 401:
        raise IamError(
            "session_expired",
            "Your AISquare session has expired or was revoked."
            + prog.shell_advice(" Run aisquare login."),
        )
    _reject_common(result, "identify you")
    if result.status != 200 or not isinstance(result.body, dict):
        raise IamError("unsupported_server", f"{endpoints.issuer} did not return your profile.")
    return result.body


def revoke(endpoints: Endpoints, session: Session) -> bool:
    """RFC 7009, as the client ``session``'s token was issued to.

    ``True`` when the server answered; a network failure is ``False``.
    """
    form = {"token": session.token, "client_id": session.client_id}
    try:
        result = _http("POST", endpoints.revocation, form=form)
    except IamError:
        return False
    return 200 <= result.status < 300


# --------------------------------------------------------------------------- the stored session


def stored_session() -> Session | None:
    data = credentials.load_all()
    token = data.get(KEY_TOKEN)
    api_url = data.get(KEY_API_URL)
    if not token or not api_url:
        return None
    return Session(
        api_url=api_url,
        token=token,
        source="file",
        expires_at=_parse_timestamp(data.get(KEY_EXPIRES_AT)),
        scope=data.get(KEY_SCOPE, ""),
        sub=data.get(KEY_SUB, ""),
        email=data.get(KEY_EMAIL, ""),
        name=data.get(KEY_NAME, ""),
        # 0.7.0 to 0.10.0 recorded aisquare-cli and never read it back. A file without
        # the key is read as theirs, which is what it was before this line read it.
        client_id=data.get(KEY_CLIENT_ID) or FALLBACK_CLIENT_ID,
    )


def env_session(api_url: str | None = None) -> Session | None:
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        return None
    return Session(api_url=api_url or resolve_api_url(), token=token, source="env")


def current_session(api_url: str | None = None) -> Session | None:
    """The environment token wins, read-only; otherwise the file."""
    return env_session(api_url) or stored_session()


def signed_in_quietly() -> Session | None:
    """The sign-in when there is one, else ``None`` — for a call that must never cost the command.

    :func:`current_session` raises when the credentials file cannot be read.
    The callers are best-effort revokes of keys the CLI minted, and one that has
    no session to revoke with leaves the key owed, not lost
    (``destinations.revoke_owed``), so an unreadable file is simply no session.
    """
    try:
        return current_session()
    except IamError:
        return None


def store_session(
    *,
    api_url: str,
    token: str,
    expires_in: int | None,
    scope: str,
    claims: dict[str, Any],
    client_id: str,
) -> Session:
    expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
    values = {
        KEY_API_URL: api_url,
        KEY_TOKEN: token,
        KEY_EXPIRES_AT: expires_at.isoformat() if expires_at else "",
        KEY_SCOPE: scope,
        KEY_SUB: str(claims.get("sub", "")),
        KEY_EMAIL: str(claims.get("email", "")),
        KEY_NAME: str(claims.get("name", "")),
        # The client the token was issued to, read back to revoke it as that client.
        # Still one session per file: a sign-in as either client replaces the other's.
        KEY_CLIENT_ID: client_id,
    }
    # store() merges only non-empty values. Replace this session's keys first
    # so an unknown expiry or omitted claim cannot survive from the old token —
    # in ONE write, not a `drop` followed by a `store`. That pair rewrote the
    # whole file twice and restricted it twice, which on Windows is four
    # `icacls` subprocesses per sign-in, each with a 15 second timeout, and two
    # separate moments with the token on disk before the DACL was applied.
    _, restricted = credentials.store(**values, replace=CREDENTIAL_KEYS)
    if not restricted:
        # The third secret in this file, and the one that had no report. The API
        # key says so through `lifecycle.initialize` and the serve token through
        # `mcp_server.serve_token`; a session token is no less worth saying out
        # loud, and this is the only place that knows. The returned session
        # carries it too, for the fleet UI, which never sees this line.
        print(f"warning: {unrestricted_warning()}", file=sys.stderr)
    return Session(
        api_url=api_url,
        token=token,
        source="file",
        expires_at=expires_at,
        scope=scope,
        sub=values[KEY_SUB],
        email=values[KEY_EMAIL],
        name=values[KEY_NAME],
        unrestricted=not restricted,
        client_id=client_id,
    )


def unrestricted_warning(*, signed_out: bool = False) -> str:
    """What to say when the credentials file could not be restricted to this account.

    Signed in, what is exposed is the session token just stored; signed out,
    it is what the rewritten file still holds (the API key, the serve token).
    """
    exposed = "the credentials left in it" if signed_out else "your session token"
    return (
        f"could not restrict {paths.credentials_path()} to your account — "
        f"other users on this machine may be able to read {exposed}."
    )


def clear_session() -> bool:
    """Forget the stored session; whether the file left behind is restricted to this account.

    Dropping the session rewrites the file that still holds the API key and
    the serve token, as a new file, and one that could not be restricted is
    worth the same word on stderr that storing them got. The answer is also
    returned, for the fleet UI, which never sees that line (review of the #65
    fold, round 2, F3); ``True`` when there was nothing to rewrite.
    """
    _, restricted = credentials.drop(*CREDENTIAL_KEYS)
    if not restricted:
        print(f"warning: {unrestricted_warning(signed_out=True)}", file=sys.stderr)
    return restricted


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def post_json(
    url: str, body: Any, *, token: str | None = None, timeout: float = HTTP_TIMEOUT_SECONDS
) -> HttpResult:
    """POST ``body`` as JSON to ``url``, as ``token``'s bearer when one is given and anonymously
    when not: for an endpoint that takes both (the usage-sharing one, ``services.usage_share``).
    **No redirect is followed**: a 3xx comes back as the result it is, and the token goes to
    ``url`` and nowhere else.

    The caller resolves ``url`` (``resolve_api_url``, which refuses plain http away from
    loopback) and decides whether ``token`` may go there; error statuses are results, and a
    transport error is ``IamError("unreachable")``, as for every call here."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return _http(
        "POST", url, json_body=body, headers=headers, timeout=timeout, follow_redirects=False
    )


# --------------------------------------------------------------------------- calling the API


def request(
    path: str,
    *,
    method: str = "GET",
    form: dict[str, str] | None = None,
    json_body: Any | None = None,
    workspace: str | None = None,
    api_url: str | None = None,
    tolerate: tuple[int, ...] = (),
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> HttpResult:
    """Call the AISquare API as the signed-in user. There is no refresh path.

    ``tolerate`` names statuses to hand back as results instead of raising:
    a 401 is normally "the session is gone", but an endpoint whose
    authentication class predates the sign-in token answers 401 to a perfectly
    live session, and the caller that knows which endpoint it is talking to is
    the one that can tell the two apart (#142).

    ``workspace`` goes out as ``X-Workspace-Id`` — the API resolves the
    workspace context from that header (an id or a workspace uid) and falls
    back to the user's personal workspace without it, never to an error, so a
    caller that means a particular workspace must always pass it (#142).
    """
    resolved = resolve_api_url(api_url)
    session = current_session(resolved)
    if session is None:
        raise IamError(
            "not_authenticated", "Not signed in." + prog.shell_advice(" Run aisquare login.")
        )
    if session.source == "file" and session.api_url != resolved:
        raise IamError(
            "api_url_mismatch",
            f"You are signed in to {session.api_url} but this command targets {resolved}."
            + prog.shell_advice(
                f" Run aisquare login --api-url {resolved}, or unset {API_URL_ENV_VAR}."
            ),
        )
    headers = {"Authorization": f"Bearer {session.token}"}
    if workspace:
        headers["X-Workspace-Id"] = workspace
    result = _http(
        method,
        f"{resolved}/{path.lstrip('/')}",
        form=form,
        json_body=json_body,
        headers=headers,
        timeout=timeout,
    )
    if result.status in tolerate:
        return result
    if result.status == 401:
        raise IamError(
            "session_expired",
            "Your AISquare session has expired or was revoked."
            + prog.shell_advice(" Run aisquare login."),
        )
    if result.status == 429:
        raise IamError(
            "rate_limited",
            f"Too many requests. Try again in {max(1, result.retry_after() // 60)} minutes.",
            retry_after=result.retry_after(),
        )
    return result
