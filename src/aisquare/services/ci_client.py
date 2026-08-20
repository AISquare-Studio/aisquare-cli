"""The single transport between aisquare's hooks and the CI endpoint.

One client, four triggers, one method. A new server-side capability must cost
zero CLI changes, so nothing here branches on what a response *means* — that is
:mod:`aisquare.services.ci_contract`'s job, and this module only gets bytes to
it and reports how long that took.

Three properties, in the order they matter:

**Off costs nothing.** ``experiment.enabled`` is False by default, and in that
state :func:`call` returns before touching the network, the config's URL, or
this module's imports of it. The ``prompt_submit`` call runs synchronously in
front of a developer who has just hit enter, so "disabled" has to mean zero
measurable latency rather than a fast failure.

**Failure is never visible to the session.** Everything resolves to an allow
outcome carrying the reason it degraded. Callers persist that reason: a
timeout and a server deliberately saying "nothing to add" are both
``action=allow``, and recorded without the reason they are the same row.

**No retries.** A retry on this path doubles the latency being measured, which
turns a slow endpoint into a slower one and contaminates the very number the
experiment exists to collect. One attempt, then degrade.

The transport is :mod:`urllib` rather than ``httpx`` deliberately. The base
install must stay unchanged and this path has to work without the
``[experiment]`` extra present, which rules out a hard third-party import on
the hot path; :mod:`aisquare.services.explainability` already probes over
:mod:`urllib` for the same reason.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from aisquare.core.config import load_config
from aisquare.services import ci_cache
from aisquare.services.ci_contract import (
    ADVISORY_BUDGET_MS,
    CLIENT_BACKSTOP_SECONDS,
    CONTRACT_HEADER,
    CONTRACT_VERSION,
    Action,
    DegradationReason,
    HookRequest,
    Outcome,
    ToolRef,
    Trigger,
    degraded,
    parse_response,
)

_ON_VALUES = {"1", "true", "yes", "on"}
_OFF_VALUES = {"0", "false", "no", "off"}

URL_ENV_VAR = "AISQUARE_CI_URL"
KEY_ENV_VAR = "AISQUARE_CI_KEY"
ENABLED_ENV_VAR = "AISQUARE_CI"

_HOOK_PATH = "/v1/hook"


@dataclass(frozen=True)
class Call:
    """One attempted call, and everything a metrics row needs from it.

    Timing is reported as two numbers rather than one. ``round_trip_ms`` minus
    the response's ``server_ms`` is the network cost; folded together, a slow
    link is indistinguishable from a slow server, and the wrong team spends a
    week on it.
    """

    outcome: Outcome
    round_trip_ms: int
    cache_hit: bool = False

    @property
    def action(self) -> Action:
        """What the CLI should do. ``allow`` whenever the call degraded."""
        return self.outcome.response.action

    @property
    def reason(self) -> DegradationReason:
        """Why there is no server decision, or ``none``."""
        return self.outcome.reason

    @property
    def degraded(self) -> bool:
        return self.outcome.degraded

    @property
    def server_ms(self) -> int | None:
        return self.outcome.response.server_ms

    @property
    def network_ms(self) -> int | None:
        """Round trip minus the server's own timing, when it reported any."""
        server_ms = self.server_ms
        return None if server_ms is None else max(0, self.round_trip_ms - server_ms)


def enabled() -> bool:
    """Whether the CI test bed is switched on. Off unless explicitly enabled.

    ``AISQUARE_CI`` wins over config in both directions so a run can be turned
    on for one session without editing a file, and off again just as fast when
    it misbehaves — the state you want reachable in a hurry.
    """
    override = os.environ.get(ENABLED_ENV_VAR, "").strip().lower()
    if override in _ON_VALUES:
        return True
    if override in _OFF_VALUES:
        return False
    return _settings_enabled()


def _settings_enabled() -> bool:
    """``experiment.enabled`` from config, False if the config is unreadable."""
    try:
        return load_config().experiment.enabled
    except Exception:  # a broken config must not enable anything
        return False


def endpoint() -> str:
    """The configured base URL, or ``""`` when there is none."""
    from_env = os.environ.get(URL_ENV_VAR, "").strip()
    if from_env:
        return from_env.rstrip("/")
    try:
        return load_config().experiment.url.strip().rstrip("/")
    except Exception:
        return ""


def api_key() -> str:
    """The bearer token. Environment only — never read from ``config.toml``."""
    return os.environ.get(KEY_ENV_VAR, "").strip()


def call(
    trigger: Trigger,
    *,
    session_id: str,
    trace_id: str,
    project_id: str,
    prompt: str | None = None,
    tool: ToolRef | None = None,
    run_id: str | None = None,
    arm: str | None = None,
    snapshot_ref: str | None = None,
    budget_ms: int = ADVISORY_BUDGET_MS,
    cache_key: str | None = None,
) -> Call:
    """Ask the endpoint what to do. Never raises, never retries.

    ``cache_key`` is consulted before any request is made and is the mechanism
    behind the ``session_start`` prefetch: a hit costs a small local file read
    instead of a synchronous round trip. Which key a given trigger should use
    is a server-side decision — see the cache-scoping entry in
    ``docs/ci-contract.md``.
    """
    if not enabled():
        return Call(degraded(DegradationReason.disabled), round_trip_ms=0)
    base = endpoint()
    if not base:
        return Call(degraded(DegradationReason.not_configured), round_trip_ms=0)

    if cache_key:
        cached = ci_cache.read(session_id, cache_key)
        if cached is not None:
            started = time.monotonic()
            outcome = parse_response(status=200, body=cached)
            return Call(outcome, round_trip_ms=_ms_since(started), cache_hit=True)

    request = HookRequest(
        trigger=trigger,
        session_id=session_id,
        trace_id=trace_id,
        project_id=project_id,
        budget_ms=budget_ms,
        run_id=run_id,
        arm=arm,
        snapshot_ref=snapshot_ref,
        prompt=prompt,
        tool=tool,
    )
    started = time.monotonic()
    outcome = _post(base + _HOOK_PATH, request)
    round_trip_ms = _ms_since(started)

    # A response that arrived only after the backstop is not a usable decision:
    # the hook has already been waiting too long, and counting it would hide
    # exactly the latency this measures. Report the breach, keep the timing.
    if round_trip_ms >= CLIENT_BACKSTOP_SECONDS * 1000 and not outcome.degraded:
        return Call(
            degraded(DegradationReason.backstop_exceeded, f"{round_trip_ms}ms"),
            round_trip_ms=round_trip_ms,
        )

    _maybe_cache(session_id, outcome)
    return Call(outcome, round_trip_ms=round_trip_ms)


def _post(url: str, request: HookRequest) -> Outcome:
    """One POST, one attempt, every failure mapped to a reason."""
    body = json.dumps(request.to_wire()).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        CONTRACT_HEADER: str(CONTRACT_VERSION),
    }
    key = api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    http_request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(http_request, timeout=CLIENT_BACKSTOP_SECONDS) as response:
            payload = response.read().decode("utf-8", errors="replace")
            return parse_response(status=response.status, body=payload)
    except urllib.error.HTTPError as exc:
        # An HTTPError *is* a response: read it so a server that explains
        # itself in the body of a 429 is not thrown away as a bare status.
        try:
            payload = exc.read().decode("utf-8", errors="replace")
        except Exception:
            payload = ""
        return parse_response(status=exc.code, body=payload)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return degraded(DegradationReason.backstop_exceeded, "connect timed out")
        return degraded(DegradationReason.transport_error, str(exc.reason))
    except TimeoutError:
        return degraded(DegradationReason.backstop_exceeded, "read timed out")
    except Exception as exc:  # the hot path must have no uncaught failure mode
        return degraded(DegradationReason.transport_error, f"{type(exc).__name__}: {exc}")


def _maybe_cache(session_id: str, outcome: Outcome) -> None:
    """Store a usable response under the key and TTL the server chose."""
    if outcome.degraded:
        return
    hint = outcome.response.cache_hint
    if hint is None:
        return
    ci_cache.write(
        session_id,
        hint.key,
        outcome.response.model_dump_json(),
        hint.ttl_s,
    )


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
