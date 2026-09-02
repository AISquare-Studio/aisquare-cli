"""The single transport between aisquare and the CI server, and its switches.

One client, one method, one attempt. Nothing here branches on what a response
*means* — that is :mod:`aisquare.services.ci_contract`'s job; this module gets
bytes to it under a deadline and reports how long that took.

Four properties, in the order they matter:

**Off costs nothing.** With ``AISQUARE_CI`` unset or off, :func:`enabled` is
the only thing that runs, and it reads one environment variable. No config, no
network, no imports beyond this module's own. The ``prompt_submit`` call runs
synchronously in front of a developer who has just hit enter, so "disabled" has
to mean zero measurable latency rather than a fast failure. **Any unrecognised
value is off**: ``AISQUARE_CI=disabled`` is the kill switch someone reaches for
in a hurry, and a kill switch that fails open on a plausible spelling is not one.

**The deadline is wall-clock, not per-socket-operation.** ``urlopen``'s
``timeout`` resets on every successful read, so a server dribbling one byte per
second holds the hook forever while never tripping it. The ceiling here — the
descriptor's ``client_safety_ms`` — bounds the whole exchange: the request runs
in a worker thread the caller waits on for exactly that long, the body is read
in bounded chunks against the same clock, and a response that lands after the
ceiling is ``deadline_exceeded`` even though it arrived.

**Failure is never visible to the session and never silent in the data.**
Every failure resolves to an outcome carrying the :class:`ClientReason` for it.
A dead endpoint and a server with nothing to say both inject nothing; recorded
without the reason they are the same row, and the experiment measures its own
plumbing while looking healthy.

**No retries, no client cache.** A retry doubles the latency being measured and
contaminates the number the experiment exists to collect (``retry_policy:
none``). A client-side response cache would make a cached turn's timing describe
a network call it never made; the server caches and reports it in
``briefing.cache``.

The transport is :mod:`urllib` rather than a third-party client deliberately:
the base install must stay unchanged and this path has to work without the
``[experiment]`` extra present.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from aisquare.core.config import ExperimentSettings, load_config
from aisquare.models import BriefingStatus, ClientReason, HookAction
from aisquare.services.ci_contract import (
    RUN_ID,
    Briefing,
    BriefingOutcome,
    HookRequest,
    Outcome,
    RecallInput,
    clip,
    degraded,
    parse_briefing,
    parse_response,
)

ENABLED_ENV_VAR = "AISQUARE_CI"
URL_ENV_VAR = "AISQUARE_CI_URL"
KEY_ENV_VAR = "AISQUARE_CI_KEY"
RUN_ENV_VAR = "AISQUARE_CI_RUN"

_ON_VALUES = frozenset({"1", "true", "yes", "on"})

MAX_BODY_BYTES = 8 * 1_048_576
"""The most a hook response may be. Past it the body is ``malformed_body``.

A briefing is a few kilobytes of items plus a context the CLI caps at 16 KB
before injecting it, so anything near this is a server bug — but a bug worth
*recording*: an oversized ``rendered_context`` that fits under this cap is read,
capped at injection, and the row keeps both sizes, which is how the server team
learns it happened. Past eight megabytes the cost of reading in front of a
waiting developer outweighs the diagnostic, and the read stops."""

_CHUNK_BYTES = 65_536


# --- switches -----------------------------------------------------------------


def enabled() -> bool:
    """Whether the test bed is switched on. Off unless explicitly enabled.

    ``AISQUARE_CI`` wins over config in both directions. An unset or empty
    variable defers to ``experiment.enabled``; a recognised on-value enables;
    **anything else is off** — the safe state, and the one a typo must land in.
    """
    override = os.environ.get(ENABLED_ENV_VAR, "").strip().lower()
    if not override:
        return _settings().enabled
    return override in _ON_VALUES


def raw_endpoint() -> str:
    """The configured base URL as written, or ``""``. For diagnostics."""
    from_env = os.environ.get(URL_ENV_VAR, "").strip()
    return from_env or _settings().url.strip()


def endpoint() -> str:
    """The base URL requests go to, or ``""`` when there is no usable one.

    ``http(s)://`` only. A scheme-less ``example.com`` used to raise out of the
    request constructor, past every reason the ladder knows — and a hook that
    raises loses its whole output, saved context included. It is now simply
    "not configured", and ``doctor`` names the missing scheme.
    """
    url = raw_endpoint().rstrip("/")
    return url if url.lower().startswith(("http://", "https://")) else ""


def api_key() -> str:
    """The bearer token. Environment only — never read from ``config.toml``."""
    return os.environ.get(KEY_ENV_VAR, "").strip()


def raw_run_id() -> str:
    """The configured run id as written, or ``""``. For diagnostics."""
    from_env = os.environ.get(RUN_ENV_VAR, "").strip()
    return from_env or _settings().run.strip()


def run_id() -> str:
    """The ``run_…`` id whose descriptor drives this session, or ``""``.

    Validated against the contract's pattern so a malformed value is "no run"
    rather than a request the server rejects on shape. The CLI never mints one.
    """
    value = raw_run_id()
    return value if RUN_ID.match(value) else ""


def _settings() -> ExperimentSettings:
    """``experiment`` from config; the defaults when the config is unreadable.

    A broken config must not enable anything, and must not cost the hook its
    output either.
    """
    try:
        return load_config().experiment
    except Exception:
        return ExperimentSettings()


# --- one HTTP exchange under a wall-clock deadline ----------------------------


@dataclass(frozen=True)
class Exchange:
    """What one HTTP exchange produced, before any interpretation.

    ``reason`` is ``None`` when a response — any status — actually arrived
    inside the deadline; otherwise it names the client-side failure and
    ``status``/``body`` are empty.
    """

    status: int | None
    body: str
    elapsed_ms: int
    reason: ClientReason | None = None
    detail: str = ""


def exchange(
    url: str,
    *,
    method: str,
    deadline_ms: int,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    max_body: int = MAX_BODY_BYTES,
) -> Exchange:
    """One request, one attempt, bounded by ``deadline_ms`` of wall clock.

    Never raises. The work runs in a daemon thread the caller joins for the
    deadline; if the thread has not finished the exchange is
    ``deadline_exceeded`` and the thread is abandoned (a short-lived hook
    process takes it down on exit). A result that arrives late — the thread
    finished, but the clock says the ceiling passed — is a breach too: the
    hook has already waited too long, and counting it would hide exactly the
    latency being measured.
    """
    started = time.monotonic()
    deadline_s = max(deadline_ms, 1) / 1000.0
    box: list[Exchange] = []

    def work() -> None:
        box.append(
            _blocking_exchange(
                url,
                method=method,
                headers=headers or {},
                body=body,
                started=started,
                deadline_s=deadline_s,
                max_body=max_body,
            )
        )

    worker = threading.Thread(target=work, name="aisquare-ci-exchange", daemon=True)
    worker.start()
    worker.join(deadline_s)
    elapsed_ms = _ms_since(started)
    if not box or late(elapsed_ms, deadline_ms):
        return Exchange(
            status=None,
            body="",
            elapsed_ms=elapsed_ms,
            reason=ClientReason.deadline_exceeded,
            detail=(
                f"no response within {deadline_ms} ms"
                if not box
                else f"response arrived at {elapsed_ms} ms, after the {deadline_ms} ms ceiling"
            ),
        )
    result = box[0]
    return Exchange(
        status=result.status,
        body=result.body,
        elapsed_ms=elapsed_ms,
        reason=result.reason,
        detail=result.detail,
    )


def late(elapsed_ms: int, deadline_ms: int) -> bool:
    """Whether a response that did arrive still counts as a deadline breach."""
    return elapsed_ms >= deadline_ms


def _blocking_exchange(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    started: float,
    deadline_s: float,
    max_body: int,
) -> Exchange:
    """The socket work, with every failure mapped to a reason. Runs off-thread."""
    try:
        request = Request(url, data=body, headers=headers, method=method)
        with urlopen(request, timeout=deadline_s) as response:
            return _read(response, response.status, started, deadline_s, max_body)
    except urllib.error.HTTPError as exc:
        # An HTTPError *is* a response: read it so a server that explains
        # itself in the body of a 429 is not thrown away as a bare status.
        try:
            return _read(exc, exc.code, started, deadline_s, max_body)
        except Exception as inner:
            return _failed(started, ClientReason.http_error, f"status {exc.code}: {inner}")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return _failed(started, ClientReason.deadline_exceeded, "connect timed out")
        return _failed(started, ClientReason.transport_error, str(exc.reason))
    except TimeoutError:
        return _failed(started, ClientReason.deadline_exceeded, "read timed out")
    except Exception as exc:  # the hot path must have no uncaught failure mode
        return _failed(started, ClientReason.transport_error, f"{type(exc).__name__}: {exc}")


def _read(response: Any, status: int, started: float, deadline_s: float, max_body: int) -> Exchange:
    """Read a body in bounded chunks under the deadline and the size cap."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_body:
            return _failed(started, ClientReason.malformed_body, f"body exceeds {max_body} bytes")
        chunks.append(chunk)
        if time.monotonic() - started >= deadline_s:
            return _failed(
                started, ClientReason.deadline_exceeded, "body still arriving at the ceiling"
            )
    return Exchange(
        status=status,
        body=b"".join(chunks).decode("utf-8", errors="replace"),
        elapsed_ms=_ms_since(started),
    )


def _failed(started: float, reason: ClientReason, detail: str) -> Exchange:
    return Exchange(
        status=None, body="", elapsed_ms=_ms_since(started), reason=reason, detail=clip(detail)
    )


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def headers_for(key: str, *, json_body: bool) -> dict[str, str]:
    """The request headers: JSON in and out, and the bearer when there is one.

    No contract header — v2 carries ``contract`` in the body, where the schema
    can check it.
    """
    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


# --- the hook call ------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One attempted hook call, and everything a metrics row needs from it.

    Timing is two numbers rather than one. ``round_trip_ms`` minus the
    response's ``server_ms`` is the network cost; folded together, a slow link
    is indistinguishable from a slow server and the wrong team spends a week
    on it.
    """

    outcome: Outcome
    round_trip_ms: int
    request: HookRequest | None = None

    @property
    def action(self) -> HookAction:
        """What the CLI should do. ``noop`` whenever the call degraded."""
        return self.outcome.action

    @property
    def reason(self) -> ClientReason:
        return self.outcome.reason

    @property
    def degraded(self) -> bool:
        return self.outcome.degraded

    @property
    def status(self) -> BriefingStatus | None:
        """The server's own verdict, when one arrived."""
        return None if self.outcome.response is None else self.outcome.response.status

    @property
    def briefing(self) -> Briefing | None:
        return self.outcome.briefing

    @property
    def server_ms(self) -> int | None:
        return None if self.outcome.response is None else self.outcome.response.server_ms

    @property
    def network_ms(self) -> int | None:
        """Round trip minus the server's own timing, when it reported any."""
        server_ms = self.server_ms
        return None if server_ms is None else self.round_trip_ms - server_ms

    @property
    def deadline_breached(self) -> bool | None:
        if self.outcome.response is None:
            return True if self.reason is ClientReason.deadline_exceeded else None
        return self.outcome.response.deadline.breached

    @property
    def error_codes(self) -> list[str]:
        """``errors[].code`` from a parsed response, or the ``error.v1`` code a
        non-200 body carried. Either way the catalog is the server's."""
        if self.outcome.response is None:
            return list(self.outcome.error_codes)
        return [error.code for error in self.outcome.response.errors]

    @property
    def config_fingerprint(self) -> str | None:
        """The envelope's fingerprint — present on ``empty`` answers too."""
        return None if self.outcome.response is None else self.outcome.response.config_fingerprint

    @property
    def detail(self) -> str:
        return self.outcome.detail


def call(request: HookRequest, *, url: str) -> Call:
    """POST ``request`` to ``url``. Never raises, never retries.

    The deadline is the request's own ``client_safety_ms`` — the same number
    the server is told — enforced as wall clock by :func:`exchange`.
    """
    payload = json.dumps(request.to_wire()).encode("utf-8")
    result = exchange(
        url,
        method="POST",
        deadline_ms=request.client_safety_ms,
        headers=headers_for(api_key(), json_body=True),
        body=payload,
        max_body=MAX_BODY_BYTES,
    )
    if result.reason is not None:
        return Call(degraded(result.reason, result.detail), result.elapsed_ms, request)
    outcome = parse_response(status=result.status or 0, body=result.body)
    return Call(outcome, result.elapsed_ms, request)


# --- the pull call ------------------------------------------------------------


@dataclass(frozen=True)
class RecallCall:
    """One attempted pull through the server's MCP route — :class:`Call`'s sibling.

    Same properties, so a metrics row is built from either without knowing
    which surface answered. Where the hook envelope would have carried a value
    and the bare briefing does not — ``action``, ``server_ms``, the server's
    own ``deadline`` block — the property says ``None`` rather than inventing
    one: a pull has no inject/noop decision (the agent asked, and gets what
    came back), and the briefing's ``timing_ms`` is the reader's clock, not
    the handler's, so it is not passed off as ``server_ms``.
    """

    outcome: BriefingOutcome
    round_trip_ms: int
    request: RecallInput | None = None

    @property
    def action(self) -> HookAction | None:
        return None

    @property
    def reason(self) -> ClientReason:
        return self.outcome.reason

    @property
    def degraded(self) -> bool:
        return self.outcome.degraded

    @property
    def status(self) -> BriefingStatus | None:
        """The server's verdict — inside the briefing on this surface."""
        return None if self.outcome.briefing is None else self.outcome.briefing.status

    @property
    def briefing(self) -> Briefing | None:
        return self.outcome.briefing

    @property
    def server_ms(self) -> int | None:
        return None

    @property
    def network_ms(self) -> int | None:
        return None

    @property
    def deadline_breached(self) -> bool | None:
        """``True`` when the client ceiling passed; otherwise unknown.

        The hook envelope reports the *server's* breach verdict and the row
        records that. The bare briefing carries none — a server-side breach
        arrives as a 200 with ``status: unavailable`` — so a pull row can say
        the client ceiling was breached, never that nothing was.
        """
        return True if self.reason is ClientReason.deadline_exceeded else None

    @property
    def error_codes(self) -> list[str]:
        return list(self.outcome.error_codes)

    @property
    def config_fingerprint(self) -> str | None:
        return None if self.outcome.briefing is None else self.outcome.briefing.config_fingerprint

    @property
    def detail(self) -> str:
        return self.outcome.detail


def recall(request: RecallInput, *, url: str, deadline_ms: int) -> RecallCall:
    """POST ``request`` to the pull route. Never raises, never retries.

    ``mcp-tool-input.v1`` carries no ceiling of its own, so the deadline is the
    descriptor's ``client_safety_ms``, passed in — the same ceiling the hooks
    run under, enforced the same way.
    """
    payload = json.dumps(request.to_wire()).encode("utf-8")
    result = exchange(
        url,
        method="POST",
        deadline_ms=deadline_ms,
        headers=headers_for(api_key(), json_body=True),
        body=payload,
        max_body=MAX_BODY_BYTES,
    )
    if result.reason is not None:
        return RecallCall(
            BriefingOutcome(None, result.reason, clip(result.detail)), result.elapsed_ms, request
        )
    return RecallCall(
        parse_briefing(status=result.status or 0, body=result.body), result.elapsed_ms, request
    )


DeliveryCall = Call | RecallCall
"""Either surface's call: what a metrics row is built from."""
