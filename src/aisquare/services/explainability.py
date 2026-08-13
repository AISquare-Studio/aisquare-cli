"""Wire an agent session through the AISquare explainability proxy.

The proxy (``aisquare.explainability.claude_proxy`` from the SDK) sits between
Claude Code and api.anthropic.com and turns each session into a Run on the
explainability dashboard. Joining it needs exactly two env vars on the agent
process: ``ANTHROPIC_BASE_URL`` pointing at the proxy, and
``ANTHROPIC_CUSTOM_HEADERS`` carrying the identity pair — ``X-Agent-Name``
(the per-role identity registered in the studio) plus ``X-Pipeline-Id`` (the
per-session run key). The pair is load-bearing: a name without a pipeline id
is silently ignored by the proxy and the run is misattributed to its default
identity, and sessions without distinct pipeline ids merge into one Run.

Everything here fails open by design. Tracing is an observer — the moment it
would break a launch (proxy down, wrong proxy mode, an ANTHROPIC_* var the
user already owns, a config typo) we launch the session untraced and say why,
we never block. The only hard opt-in is ``explainability.enabled`` in
``~/.aisquare/config.toml``, which defaults to off.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.error import URLError
from urllib.request import urlopen

from aisquare.core.config import ExplainabilitySettings

#: Vars the wiring wants to set. If the user's environment already defines one
#: they are routing Anthropic traffic deliberately (custom gateway, another
#: observability layer) — clobbering it would silently redirect or untrace
#: THEIR setup, so we stand down instead.
_RESERVED_ENV_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS")

#: The proxy /health contract this wiring was verified against: any service or
#: mode mismatch means "some other listener owns that port" (e.g. a creator-mode
#: proxy), and pointing a Claude Code session at it is not a risk worth taking.
_EXPECTED_SERVICE = "aisquare-proxy"
_EXPECTED_MODE = "claude_code"

#: Role names travel inside an HTTP header value; anything beyond this set is
#: either a typo or an injection attempt, and both fail open.
_SAFE_ROLE = re.compile(r"^[A-Za-z0-9._-]+$")

_PROBE_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True)
class ProxyProbe:
    """Outcome of one ``GET {proxy_url}/health`` check."""

    healthy: bool
    reason: str


@dataclass(frozen=True)
class SessionWiring:
    """The env delta for one session launch, and why it is (or isn't) traced."""

    traced: bool
    reason: str
    env: dict[str, str] = field(default_factory=dict)
    agent_name: str | None = None
    pipeline_id: str | None = None


def probe_proxy(proxy_url: str, timeout: float = _PROBE_TIMEOUT_SECONDS) -> ProxyProbe:
    """Check that ``proxy_url`` is the claude_code explainability proxy.

    A 200 alone is not health here: the wrong service on the right port (the
    SDK ships several proxy modes) records traffic under the wrong contract,
    which is worse than not recording — so the service and mode names in the
    payload are part of the check.
    """
    url = proxy_url.rstrip("/") + "/health"
    try:
        with urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return ProxyProbe(False, f"proxy /health returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, TimeoutError, ValueError) as exc:
        return ProxyProbe(False, f"proxy unreachable at {url}: {exc}")
    service = payload.get("service")
    mode = payload.get("mode")
    if service != _EXPECTED_SERVICE:
        return ProxyProbe(False, f"{url} answers as {service!r}, not the explainability proxy")
    if mode != _EXPECTED_MODE:
        return ProxyProbe(
            False,
            f"proxy at {url} runs mode {mode!r}, need {_EXPECTED_MODE!r} — "
            "point explainability.proxy_url at the claude_code proxy",
        )
    return ProxyProbe(True, "proxy healthy")


def wire_session(
    settings: ExplainabilitySettings,
    role: str,
    *,
    session_id: str | None = None,
    base_env: dict[str, str] | None = None,
    prober: Callable[[str], ProxyProbe] = probe_proxy,
) -> SessionWiring:
    """Build the env delta that traces one session, or explain why not.

    ``session_id`` becomes the run's ``X-Pipeline-Id`` when given — pass the
    agent session id so board rows and dashboard Runs share a key; otherwise a
    fresh UUID keeps concurrent sessions from merging into one Run. ``base_env``
    is consulted (never mutated) for vars the user already owns.
    """
    if not settings.enabled:
        return SessionWiring(traced=False, reason="explainability is disabled (config default)")

    if not _SAFE_ROLE.match(role or ""):
        return SessionWiring(
            traced=False, reason=f"role {role!r} is not header-safe — launching untraced"
        )

    if base_env:
        taken = [name for name in _RESERVED_ENV_VARS if base_env.get(name)]
        if taken:
            return SessionWiring(
                traced=False,
                reason=f"{' and '.join(taken)} already set — not overriding your "
                "routing, launching untraced",
            )

    try:
        agent_name = settings.agent_name_template.format(role=role)
    except (KeyError, IndexError, ValueError) as exc:
        return SessionWiring(
            traced=False,
            reason=f"agent_name_template {settings.agent_name_template!r} is "
            f"invalid ({exc}) — launching untraced",
        )
    if not _SAFE_ROLE.match(agent_name):
        return SessionWiring(
            traced=False,
            reason=f"agent name {agent_name!r} is not header-safe — launching untraced",
        )

    verdict = prober(settings.proxy_url)
    if not verdict.healthy:
        return SessionWiring(traced=False, reason=f"{verdict.reason} — launching untraced")

    pipeline_id = session_id or str(uuid.uuid4())
    return SessionWiring(
        traced=True,
        reason=f"traced as {agent_name} (pipeline {pipeline_id})",
        env={
            "ANTHROPIC_BASE_URL": settings.proxy_url,
            "ANTHROPIC_CUSTOM_HEADERS": (
                f"X-Agent-Name: {agent_name}\nX-Pipeline-Id: {pipeline_id}"
            ),
        },
        agent_name=agent_name,
        pipeline_id=pipeline_id,
    )
