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

import importlib
import importlib.util
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from aisquare.core import insights, outbox, paths
from aisquare.core.config import ExplainabilitySettings, load_config, save_config
from aisquare.core.store import store_session

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


# ---------------------------------------------------------------------------
# The client lane: insights the CLI holds, shipped to the gateway.
#
# The proxy above sees model traffic. It cannot see the other half — what the
# human typed, what the board recorded, which task a session claimed — because
# none of that ever touches the model API. This half spools those locally (see
# aisquare.core.outbox) and delivers them here, out of process, keyed to the
# same Run as the proxy's spans.
# ---------------------------------------------------------------------------

#: Where the workspace credential lives when it is not in the environment.
#: Deliberately NOT config.toml: that file is a settings file people paste into
#: issues and copy between machines.
KEY_ENV_VAR = "EXPLAINABILITY_API_KEY"
GATEWAY_ENV_VAR = "EXPLAINABILITY_GATEWAY_URL"
AGENT_NAME_ENV_VAR = "AISQUARE_AGENT_NAME"
AGENTS_ENV_VAR = "EXPLAINABILITY_AGENTS"

#: The SDK module the extra provides. Probed by name and imported lazily: it
#: pulls opentelemetry and httpx, which is hundreds of milliseconds we refuse
#: to spend on any path a human is waiting on.
SDK_MODULE = "aisquare.explainability"

#: How the extra is installed. NOT a bare `pip install aisquare[explainability]`
#: into an env that already has this CLI: the two distributions share
#: aisquare/__init__.py and the last writer wins it. We survive either winner
#: (nothing in this package reads a name out of the top-level __init__ — see
#: aisquare/core/version.py), but an operator following a copy-pasted line into
#: a broken shell state is a bad morning, so the advice names the CLI first and
#: lets pip resolve both in one transaction.
INSTALL_HINT = 'pip install --upgrade "aisquare-cli[explainability]"'


def key_path() -> Path:
    """File holding the workspace ingest key (mode 600)."""
    return paths.aisquare_home() / "explainability-key"


def resolve_api_key() -> str | None:
    """The workspace key, from the environment or the key file.

    The environment wins so a shell that sourced an ops env file ships to the
    workspace that shell is pointed at, rather than to whatever was configured
    on this machine months ago.
    """
    from_env = os.environ.get(KEY_ENV_VAR, "").strip()
    if from_env:
        return from_env
    try:
        stored = key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return stored or None


def store_api_key(key: str) -> Path:
    """Write the workspace key at mode 600 and return where it landed."""
    target = key_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(key.strip(), encoding="utf-8")
    target.chmod(0o600)
    return target


def sdk_available() -> bool:
    """Whether the explainability extra is installed — without importing it."""
    try:
        return importlib.util.find_spec(SDK_MODULE) is not None
    except (ImportError, ValueError):
        return False


@dataclass(frozen=True)
class ShippingState:
    """Everything ``status`` and ``doctor`` need to say about the client lane."""

    configured: bool
    gateway_url: str
    has_key: bool
    sdk_installed: bool
    queued: int
    sent: int
    dead: int
    reason: str


def shipping_state() -> ShippingState:
    """Describe the client lane as it stands right now."""
    settings = load_config().explainability
    has_key = resolve_api_key() is not None
    sdk = sdk_available()
    counts = outbox.counts()
    if not settings.ship:
        reason = "off — nothing is captured (aisquare init --explainability to turn it on)"
    elif not settings.gateway_url:
        reason = f"on, but no gateway URL is configured ({GATEWAY_ENV_VAR})"
    elif not has_key:
        reason = f"on, but no workspace key — set {KEY_ENV_VAR} or write {key_path()}"
    elif not sdk:
        reason = f"on, buffering — the explainability extra is missing: {INSTALL_HINT}"
    elif counts.queued:
        reason = f"on — {counts.queued} buffered, run 'aisquare explainability ship' to drain"
    else:
        reason = "on — nothing buffered"
    return ShippingState(
        configured=settings.ship,
        gateway_url=settings.gateway_url,
        has_key=has_key,
        sdk_installed=sdk,
        queued=counts.queued,
        sent=counts.sent,
        dead=counts.dead,
        reason=reason,
    )


def configure_shipping(
    *,
    gateway_url: str | None = None,
    api_key: str | None = None,
) -> ShippingState:
    """Turn the client lane on, but only once it can actually work.

    ``ship`` is the single predicate the primary path consults, so it must never
    be True in a state that would buffer forever. Both halves are therefore
    resolved BEFORE it is written, and a missing half leaves the flag alone —
    which is also what makes "no key/config ⇒ nothing captured" true by
    construction rather than by vigilance.
    """
    config = load_config()
    url = (gateway_url or os.environ.get(GATEWAY_ENV_VAR, "")).strip()
    if url:
        config.explainability.gateway_url = url
    if api_key:
        store_api_key(api_key)
    resolved_key = resolve_api_key()
    if config.explainability.gateway_url and resolved_key:
        config.explainability.ship = True
    save_config(config)
    insights.reset_cache()
    return shipping_state()


def disable_shipping() -> ShippingState:
    """Stop capturing for the gateway. The spool is left alone, not deleted."""
    config = load_config()
    config.explainability.ship = False
    save_config(config)
    insights.reset_cache()
    return shipping_state()


@dataclass(frozen=True)
class ShipReport:
    """Outcome of one drain of the spool."""

    sent: int = 0
    deferred: int = 0
    dead: int = 0
    runs: tuple[str, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.dead == 0 and self.deferred == 0


def ship_once(limit: int = 500) -> ShipReport:
    """Drain the spool into the gateway. Safe to run concurrently, and to kill.

    Records are grouped by session and replayed under ONE ``AgentRunTracer``
    per session, keyed with ``run_id`` = the board session id — the same value
    the proxy sends as ``X-Pipeline-Id``. That is the whole correlation spine:
    a session's model traffic and its human/board insights land in one Run
    because both name it the same way, and a second drain of the same session
    re-opens that Run rather than fragmenting it.

    Never raises. A drain that cannot run leaves every record queued and says
    why — buffering is the correct behaviour for an unreachable gateway.
    """
    settings = load_config().explainability
    if not settings.ship:
        return ShipReport(reason="shipping is not configured — nothing to do")
    if not settings.gateway_url:
        return ShipReport(reason=f"no gateway URL configured ({GATEWAY_ENV_VAR})")
    api_key = resolve_api_key()
    if api_key is None:
        return ShipReport(reason=f"no workspace key — set {KEY_ENV_VAR} or write {key_path()}")
    if not sdk_available():
        pending = len(outbox.pending())
        return ShipReport(
            deferred=pending,
            reason=f"explainability extra not installed, {pending} buffered — {INSTALL_HINT}",
        )

    outbox.reclaim_stale()
    batch = outbox.pending(limit)
    if not batch:
        return ShipReport(reason="nothing buffered")

    try:
        sdk = _init_sdk(settings, api_key)
    except Exception as exc:  # the SDK failing to start is a deferral, not a loss
        return ShipReport(
            deferred=len(batch), reason=f"explainability SDK would not initialise: {exc}"
        )

    return _drain(sdk, settings, batch)


def _init_sdk(settings: ExplainabilitySettings, api_key: str) -> Any:
    """Import and initialise the SDK from an environment we control.

    ``init_from_env`` is the SDK's own contract, so we set the variables it
    reads rather than reaching past it into private setters — an SDK upgrade
    that changes its internals must not silently stop shipping.
    """
    os.environ[GATEWAY_ENV_VAR] = settings.gateway_url
    os.environ[KEY_ENV_VAR] = api_key
    sdk = importlib.import_module(SDK_MODULE)
    sdk.init_from_env(auto_instrument=False)
    return sdk


def _drain(sdk: Any, settings: ExplainabilitySettings, batch: list[Path]) -> ShipReport:
    sent = 0
    deferred = 0
    dead = 0
    runs: list[str] = []
    for run_key, paths_for_run in _group_by_session(batch).items():
        agent_name = _agent_name_for(settings, run_key)
        claimed: list[tuple[Path, dict[str, object]]] = []
        for path in paths_for_run:
            handle = outbox.claim(path)
            if handle is None:  # another sweeper got there first
                continue
            record = outbox.load(handle)
            if record is None or record.get("v") != insights.RECORD_VERSION:
                outbox.mark_dead(handle, "unreadable or unsupported record")
                dead += 1
                continue
            claimed.append((handle, record))
        if not claimed:
            continue
        try:
            with sdk.AgentRunTracer(agent_name=agent_name, run_id=run_key) as run:
                run.set_input(f"aisquare-cli session {run_key}")
                for _, record in claimed:
                    _emit_span(sdk, record)
                run.set_status("completed")
            sdk.flush()
        except Exception as exc:  # every failure mode is a deferral
            for handle, _ in claimed:
                outbox.release(handle)
            deferred += len(claimed)
            return ShipReport(
                sent=sent,
                deferred=deferred,
                dead=dead,
                runs=tuple(runs),
                reason=f"gateway unreachable, {deferred} still buffered: {exc}",
            )
        for handle, _ in claimed:
            outbox.mark_sent(handle)
        sent += len(claimed)
        runs.append(run_key)
    return ShipReport(
        sent=sent,
        deferred=deferred,
        dead=dead,
        runs=tuple(runs),
        reason=f"{sent} shipped across {len(runs)} run(s)"
        + (f", {dead} dead-lettered" if dead else ""),
    )


def _emit_span(sdk: Any, record: dict[str, object]) -> None:
    """Replay one spooled record as a span inside the open Run."""
    text = str(record.get("text") or "")
    if record.get("kind") == "prompt":
        with sdk.HumanInterventionTracer(
            human_id=str(record.get("session_id") or "unknown"),
            action="prompt",
            reason=text,
        ):
            return
    kind = str(record.get("event_kind") or "team_event")
    with sdk.DecisionTracer(decision_type=f"board.{kind}") as decision:
        # seq is the join key back to the board row; without it a reader of the
        # Run can see WHAT was decided but never find the row that recorded it.
        decision.set_selected(text, reason=f"board seq {record.get('seq')}")


def _group_by_session(batch: list[Path]) -> dict[str, list[Path]]:
    """Bucket spooled records by the Run they belong to, order preserved."""
    grouped: dict[str, list[Path]] = {}
    for path in batch:
        record = outbox.load(path)
        run_key = str((record or {}).get("session_id") or "") or UNATTRIBUTED_RUN
        grouped.setdefault(run_key, []).append(path)
    return grouped


#: Records with no session id (a plain `aisquare note` from a shell, say) still
#: belong somewhere. One bucket rather than one Run each: a hundred one-span
#: Runs is exactly the fragmentation the run doctrine forbids.
UNATTRIBUTED_RUN = "aisquare-cli-unattributed"


def _agent_name_for(settings: ExplainabilitySettings, run_key: str) -> str:
    """The registered identity this Run is attributed to.

    Resolved from the session's board role, so a planner's Run is not a coder's
    — the gateway collapses runs and costs under a shared name. Looking the
    role up costs a store read, which is fine HERE: the sweeper is off the
    primary path, which is the entire reason it exists.
    """
    role = "cli"
    if run_key != UNATTRIBUTED_RUN:
        try:
            with store_session() as store:
                session = store.get_session(run_key)
            if session is not None and session.role:
                role = session.role
        except Exception:  # an unknown role still ships, as "cli"
            role = "cli"
    try:
        name = settings.agent_name_template.format(role=role)
    except (KeyError, IndexError, ValueError):
        name = f"aisquare-{role}"
    return name if _SAFE_ROLE.match(name) else "aisquare-cli"


@dataclass(frozen=True)
class ShippingOffer:
    """Whether ``init`` can offer the explainability step, and what it would do."""

    available: bool
    reason: str
    gateway_url: str = ""
    has_key: bool = False
    sdk_installed: bool = False

    #: One line, shown before anyone opts in. #50 asks for this explicitly:
    #: a person must be able to read what leaves their machine BEFORE they
    #: agree to it, not discover it in a dashboard afterwards.
    CAPTURES = (
        "your prompts, board notes, task claims and session events "
        "(no file contents, no model traffic)"
    )


def shipping_offer() -> ShippingOffer:
    """Can this machine be offered the explainability step, and on what terms?

    Offered only when the extra is installed AND a gateway is discoverable —
    an offer that cannot be accepted is noise, and an offer accepted into a
    half-configured state is a queue that fills forever.
    """
    sdk = sdk_available()
    gateway = (
        os.environ.get(GATEWAY_ENV_VAR, "").strip() or load_config().explainability.gateway_url
    )
    has_key = resolve_api_key() is not None
    if not sdk:
        return ShippingOffer(
            available=False,
            reason=f"explainability extra not installed ({INSTALL_HINT})",
            gateway_url=gateway,
            has_key=has_key,
        )
    if not gateway:
        return ShippingOffer(
            available=False,
            reason=f"no gateway URL — set {GATEWAY_ENV_VAR}",
            gateway_url="",
            has_key=has_key,
            sdk_installed=True,
        )
    if not has_key:
        return ShippingOffer(
            available=False,
            reason=f"no workspace key — set {KEY_ENV_VAR}",
            gateway_url=gateway,
            has_key=False,
            sdk_installed=True,
        )
    return ShippingOffer(
        available=True,
        reason=f"ready to ship to {gateway}",
        gateway_url=gateway,
        has_key=True,
        sdk_installed=True,
    )
