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
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from aisquare.core import credentials, paths
from aisquare.core.config import load_config
from aisquare.core.version import __version__

CLIENT_ID = "aisquare-cli"
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
        raw = self.headers.get("retry-after") or self.headers.get("Retry-After") or ""
        try:
            return max(1, int(raw))
        except ValueError:
            return default

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
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> HttpResult:
    """One request. HTTP error statuses come back as results; transport errors raise."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    data = urlencode(form).encode("utf-8") if form is not None else None
    request_headers = {"Accept": "application/json", "User-Agent": user_agent()}
    if data is not None:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request_headers.update(headers or {})
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResult(response.status, _parse(response.read()), dict(response.headers))
    except HTTPError as exc:
        return HttpResult(exc.code, _parse(exc.read()), dict(exc.headers))
    except (URLError, OSError, TimeoutError, ValueError) as exc:
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
    return paths.cache_dir() / "oidc" / f"{host}{port}.json"


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


def _reject_common(result: HttpResult, what: str) -> None:
    if result.status == 429:
        raise IamError(
            "rate_limited",
            "Too many sign-in attempts from this network. "
            f"Try again in {max(1, result.retry_after() // 60)} minutes.",
            retry_after=result.retry_after(),
        )
    if result.status == 503:
        raise IamError("paused", "CLI sign-in is temporarily paused. Try again later.")
    if result.status >= 500:
        raise IamError("unreachable", f"The server could not {what} (HTTP {result.status}).")


def start_device_authorization(endpoints: Endpoints) -> DeviceAuthorization:
    result = _http(
        "POST",
        endpoints.device_authorization,
        form={"client_id": CLIENT_ID, "scope": SCOPE},
        headers={"X-Device-Name": device_name()},
    )
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
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise IamError(
            "unsupported_server", f"{endpoints.issuer} returned an incomplete device response."
        ) from exc


def poll_token(endpoints: Endpoints, device_code: str) -> PollOutcome:
    result = _http(
        "POST",
        endpoints.token,
        form={"grant_type": DEVICE_GRANT, "device_code": device_code, "client_id": CLIENT_ID},
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
            "Your AISquare session has expired or was revoked. Run aisquare login.",
        )
    _reject_common(result, "identify you")
    if result.status != 200 or not isinstance(result.body, dict):
        raise IamError("unsupported_server", f"{endpoints.issuer} did not return your profile.")
    return result.body


def revoke(endpoints: Endpoints, token: str) -> bool:
    """RFC 7009. ``True`` when the server answered; a network failure is ``False``."""
    try:
        result = _http("POST", endpoints.revocation, form={"token": token, "client_id": CLIENT_ID})
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
    )


def env_session(api_url: str | None = None) -> Session | None:
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        return None
    return Session(api_url=api_url or resolve_api_url(), token=token, source="env")


def current_session(api_url: str | None = None) -> Session | None:
    """The environment token wins, read-only; otherwise the file."""
    return env_session(api_url) or stored_session()


def store_session(
    *,
    api_url: str,
    token: str,
    expires_in: int | None,
    scope: str,
    claims: dict[str, Any],
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
        # Recorded, not read back yet: a second client_id in this file would mean
        # a second stored session, and the reader that handles that is not here.
        KEY_CLIENT_ID: CLIENT_ID,
    }
    credentials.store(**values)
    return Session(
        api_url=api_url,
        token=token,
        source="file",
        expires_at=expires_at,
        scope=scope,
        sub=values[KEY_SUB],
        email=values[KEY_EMAIL],
        name=values[KEY_NAME],
    )


def clear_session() -> None:
    credentials.drop(*CREDENTIAL_KEYS)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- calling the API


def request(
    path: str,
    *,
    method: str = "GET",
    form: dict[str, str] | None = None,
    workspace: str | None = None,
    api_url: str | None = None,
) -> HttpResult:
    """Call the AISquare API as the signed-in user. There is no refresh path."""
    resolved = resolve_api_url(api_url)
    session = current_session(resolved)
    if session is None:
        raise IamError("not_authenticated", "Not signed in. Run aisquare login.")
    if session.source == "file" and session.api_url != resolved:
        raise IamError(
            "api_url_mismatch",
            f"You are signed in to {session.api_url} but this command targets {resolved}. "
            f"Run aisquare login --api-url {resolved}, or unset {API_URL_ENV_VAR}.",
        )
    headers = {"Authorization": f"Bearer {session.token}"}
    if workspace:
        headers["X-Workspace-Id"] = workspace
    result = _http(method, f"{resolved}/{path.lstrip('/')}", form=form, headers=headers)
    if result.status == 401:
        raise IamError(
            "session_expired",
            "Your AISquare session has expired or was revoked. Run aisquare login.",
        )
    if result.status == 429:
        raise IamError(
            "rate_limited",
            f"Too many requests. Try again in {max(1, result.retry_after() // 60)} minutes.",
            retry_after=result.retry_after(),
        )
    return result
