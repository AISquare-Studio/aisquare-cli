"""The operator surface for explainability: targets, probes, and doctor checks.

This is the half a human touches — ``aisquare doctor``'s explainability
section, ``explainability enable``, ``explainability register`` — as opposed to
``services/explainability.py``, which wires a launching session's environment.

Three rules shape everything here.

**Secrets are named, never held.** Config carries the *name* of an environment
variable (``api_key_env``), and the key is read from the environment at the
moment of the call. No key, and no path to a file holding one, is ever written
to config, logged, printed, or baked into source — a doctor that echoes a
workspace key into a terminal scrollback has leaked it.

**Checks are read-mostly and offline by default.** ``aisquare doctor`` has
never made a network call; keeping it that way means an operator on a plane
still gets an answer, and the suite stays hermetic. Everything that touches the
gateway lives behind ``--live``, which is what the cutover runs.

**A red line without its next command is half a doctor.** Every non-ok check
carries the exact thing to type.

The SDK is consumed rather than reimplemented (its ``explainability-doctor``
already knows connectivity, identity and inbox backlog), and it is consumed
*defensively*: the SDK publishes as distribution ``aisquare`` and ships a
regular import package of that name, which collides with this CLI's own
``aisquare`` package in a shared environment. So we prefer the SDK's console
script over an in-process import, and check for the collision explicitly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aisquare.core.config import (
    ExplainabilitySettings,
    ExplainabilityTarget,
    load_config,
    save_config,
)
from aisquare.models import CheckStatus, DoctorCheck, RedactionLevel
from aisquare.services.explainability import (
    FALLBACK_ROLE,
    ProxyProbe,
    install_hint,
    probe_proxy,
)

#: Distribution that provides the SDK, and the console script it installs. The
#: script is the collision-free way to reach it: it runs in whatever
#: environment the SDK was installed into, ours or not.
_SDK_DIST = "aisquare"
_SDK_SCRIPT = "explainability-doctor"
_SDK_MODULE = "aisquare.explainability"

#: What to type when the SDK is missing. Quoted for shells that glob brackets.
INSTALL_HINT = 'pip install "aisquare[explainability]"'

#: Override the configured target for one command, e.g. during a cutover:
#: ``AISQUARE_EXPLAINABILITY_TARGET=prod aisquare doctor --live``.
TARGET_ENV_VAR = "AISQUARE_EXPLAINABILITY_TARGET"

#: The SDK's own name for the gateway; accepted as a fallback so a shell that
#: has already sourced the operator's env file needs no second source of truth.
GATEWAY_ENV_VAR = "EXPLAINABILITY_GATEWAY_URL"

#: Gateway-side checks are opt-in, so they may take a beat — but never hang a
#: terminal. Chosen over the proxy probe's 1.5s because this is a real WAN hop.
_HTTP_TIMEOUT = 6.0
_SDK_DOCTOR_TIMEOUT = 30.0

#: SDK doctor lines that are expected noise for this lane rather than findings:
#: the Agno adapter is a framework we do not use, and OPENAI_API_KEY is the
#: gateway's own RML-extraction credential, not a client-side setting.
_SDK_NOISE = frozenset({"agno", "openinference_agno", "openai_api_key"})

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SDK_LINE = re.compile(r"^(?P<name>\S+)\s+\[\s*(?P<status>[A-Z]+)\s*\]\s*(?P<detail>.*)$")


# ── targets ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedTarget:
    """One deployment's effective settings, with the key read but never shown."""

    name: str
    gateway_url: str
    gateway_source: str  # "config" | "env" | "unset" — shown, so surprises are visible
    api_key_env: str
    api_key: str | None
    proxy_url: str
    proxy_source: str  # "config" | "default" — the default is unreachable ON PURPOSE
    agent_name_template: str
    studio_id: str
    roles: tuple[str, ...]

    @property
    def configured(self) -> bool:
        """True when this machine knows where to ship and with what key."""
        return bool(self.gateway_url and self.api_key)

    @property
    def agent_names(self) -> tuple[str, ...]:
        """Registered identities for this target, rendered from the template.

        A template that cannot render a role is skipped rather than raised on:
        the caller is a diagnostic, and a config typo must not stop it from
        reporting the other nine things that are wrong.
        """
        names: list[str] = []
        # FALLBACK_ROLE last and deduped: the ship path emits it whenever the
        # board cannot say whose a Run is, and an identity the gateway does not
        # know is rejected 409 no_agent_identity — permanently, not
        # transiently. The three role names keep their order because §1a of the
        # cutover runbook quotes this list.
        for role in (*self.roles, FALLBACK_ROLE):
            try:
                name = self.agent_name_template.format(role=role)
            except (KeyError, IndexError, ValueError):
                continue
            if name not in names:
                names.append(name)
        return tuple(names)


def resolve_target(
    settings: ExplainabilitySettings,
    name: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> ResolvedTarget:
    """Fold the active target's overrides onto the top-level defaults.

    Precedence for the gateway URL is config, then the SDK's environment
    variable — config wins so that a shell which sourced staging credentials
    cannot silently redirect a machine configured for production, and the
    winning source is reported either way.
    """
    environ = os.environ if env is None else env
    chosen = name or environ.get(TARGET_ENV_VAR) or settings.target
    target = settings.targets.get(chosen, ExplainabilityTarget())

    gateway_url, source = target.gateway_url, "config"
    if not gateway_url:
        gateway_url, source = environ.get(GATEWAY_ENV_VAR, ""), "env"
    if not gateway_url:
        source = "unset"

    roles = target.roles if target.roles is not None else settings.roles
    return ResolvedTarget(
        name=chosen,
        gateway_url=gateway_url.rstrip("/"),
        gateway_source=source,
        api_key_env=target.api_key_env,
        api_key=environ.get(target.api_key_env) or None,
        proxy_url=target.proxy_url or settings.proxy_url,
        proxy_source=_proxy_source(settings, target),
        agent_name_template=target.agent_name_template or settings.agent_name_template,
        studio_id=target.studio_id,
        roles=tuple(roles),
    )


def effective_settings(
    settings: ExplainabilitySettings,
    name: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> ExplainabilitySettings:
    """``settings`` with the active target's overrides folded into the top level.

    The session-wiring path (``wire_session``) reads ``proxy_url`` and
    ``agent_name_template`` off the settings object directly, and knows nothing
    about targets. Without this fold, ``enable --target prod --proxy-url …``
    would write a value that every launch then ignored — config that looks
    applied and is not, which is worse than config that is missing.
    """
    target = resolve_target(settings, name, env=env)
    return settings.model_copy(
        update={
            "proxy_url": target.proxy_url,
            "agent_name_template": target.agent_name_template,
        }
    )


# ── the SDK, at arm's length ─────────────────────────────────────────────────


@dataclass(frozen=True)
class SdkPresence:
    """Where the SDK is (or isn't) reachable from, and whether it shadows us."""

    importable: bool
    script: str | None
    version: str | None
    shadowing: bool

    @property
    def present(self) -> bool:
        return self.importable or self.script is not None


def sdk_presence() -> SdkPresence:
    """Look for the SDK without importing it.

    ``find_spec`` is enough to answer "installed?" and costs none of the
    httpx/OpenTelemetry import time that touching the package would.
    """
    try:
        importable = importlib.util.find_spec(_SDK_MODULE) is not None
    except (ImportError, ValueError):  # a half-installed or shadowed parent
        importable = False
    try:
        sdk_version: str | None = version(_SDK_DIST)
    except PackageNotFoundError:
        sdk_version = None
    return SdkPresence(
        importable=importable,
        script=shutil.which(_SDK_SCRIPT),
        version=sdk_version,
        shadowing=root_package_shadowed(),
    )


def root_package_shadowed() -> bool:
    """True when the ``aisquare`` package root in this interpreter is not ours.

    Both distributions ship a regular package named ``aisquare``, so they land
    in one directory and the last writer owns ``__init__.py``. Ours always
    defines ``__version__``; if that attribute is gone, either the SDK's
    ``__init__`` is in force or (after ``pip uninstall aisquare``) the file is
    gone entirely and the name resolved as a namespace package. The CLI is
    built to survive both, but the operator should still be told, because the
    reverse case — an SDK caller reaching for ``aisquare.GovernedAgent`` — is
    broken by the same collision and is not ours to fix.
    """
    try:
        import aisquare
    except ImportError:  # pragma: no cover - we are running from it
        return False
    return getattr(aisquare, "__version__", None) is None


def sdk_doctor(*, env: Mapping[str, str] | None = None) -> list[tuple[str, str, str]]:
    """Run the SDK's own doctor and return its ``(name, status, detail)`` rows.

    Prefers the console script — that reaches an SDK installed anywhere on the
    machine and cannot drag its dependency tree into this process — and falls
    back to an in-process call when only the module is present. Returns an
    empty list when the SDK is absent or misbehaves: its checks are additive,
    and none of them may take the CLI's own diagnostics down with them.
    """
    presence = sdk_presence()
    if presence.script:
        try:
            completed = subprocess.run(
                [presence.script],
                capture_output=True,
                text=True,
                timeout=_SDK_DOCTOR_TIMEOUT,
                env=dict(env) if env is not None else None,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return _parse_sdk_table(completed.stdout)
    if not presence.importable:
        return []
    try:
        # Not a declared dependency and deliberately not stubbed: the whole
        # point is that this import may not resolve, which the guard above and
        # the except below both handle.
        from aisquare.explainability.doctor import run_doctor  # type: ignore[import-not-found]

        return [(str(n), str(s), str(d)) for n, s, d in run_doctor()]
    except Exception:  # diagnostics must never crash
        return []


def _parse_sdk_table(output: str) -> list[tuple[str, str, str]]:
    """Read the SDK doctor's printed table back into rows.

    Its ``main()`` prints ``name [ STATUS ] detail`` with ANSI colour and two
    rule lines; anything that does not match that shape is a banner, not a
    check.
    """
    rows: list[tuple[str, str, str]] = []
    for raw in _ANSI.sub("", output).splitlines():
        match = _SDK_LINE.match(raw.strip())
        if match:
            rows.append(
                (
                    match["name"],
                    match["status"].lower(),
                    match["detail"].strip(),
                )
            )
    return rows


# ── gateway probes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HttpVerdict:
    """One gateway call's outcome, in the terms a check needs."""

    ok: bool
    status: int | None
    detail: str
    code: str | None = None  # the gateway's machine-readable reason, when it sends one
    payload: Any = None


def _request(
    url: str,
    *,
    api_key: str | None = None,
    body: Any = None,
    timeout: float = _HTTP_TIMEOUT,
) -> HttpVerdict:
    """One HTTP call, with every failure turned into a verdict.

    ``X-API-KEY`` alone, deliberately: the gateway sits behind a layer that
    tries to verify any ``Authorization`` header as a JWT and fails the whole
    call when it cannot — so sending both headers is strictly worse than
    sending one, and the wrong-auth shape is unreachable from this function.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-API-KEY"] = api_key
    request = Request(url, data=data, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return HttpVerdict(
                ok=200 <= response.status < 300,
                status=response.status,
                detail=f"HTTP {response.status}",
                payload=_maybe_json(raw),
            )
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        payload = _maybe_json(raw)
        return HttpVerdict(
            ok=False,
            status=exc.code,
            detail=f"HTTP {exc.code}: {_summarise(raw)}",
            code=_gateway_code(payload),
            payload=payload,
        )
    except (URLError, OSError, TimeoutError, ValueError) as exc:
        return HttpVerdict(ok=False, status=None, detail=f"unreachable: {exc}")


def _maybe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _summarise(raw: str, limit: int = 160) -> str:
    flat = " ".join(raw.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _gateway_code(payload: Any) -> str | None:
    """Pull the gateway's machine-readable reason out of an error body.

    A 409 is three different problems with three different fixes
    (``no_agent_identity``, ``agent_not_registered``, ``awaiting_trace_route``),
    so the code, not the status, is what a remediation can be keyed on.
    """
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            for key in ("code", "reason", "error"):
                value = detail.get(key)
                if isinstance(value, str):
                    return value
        if isinstance(detail, str):
            for known in (
                "no_agent_identity",
                "agent_not_registered",
                "awaiting_trace_route",
            ):
                if known in detail:
                    return known
    return None


def probe_ready(gateway_url: str, *, timeout: float = _HTTP_TIMEOUT) -> HttpVerdict:
    """``GET {gateway}/ready`` — is the gateway up and serving?"""
    return _request(f"{gateway_url.rstrip('/')}/ready", timeout=timeout)


def probe_ingest(
    target: ResolvedTarget,
    agent_name: str,
    *,
    timeout: float = _HTTP_TIMEOUT,
) -> HttpVerdict:
    """Ship one throwaway span and report what the gateway did with it.

    This is the only check that proves the whole path at once: a 202 means the
    key was accepted AND the identity routed AND ingest is healthy. It is also
    the only honest one — the workspace key cannot read runs back (studio reads
    answer 403), so a receipt has to be a write.

    The span is shaped the way the SDK's ``AgentRunTracer`` shapes a root span
    (``AgentRun:{name}`` carrying ``agent.name``), because routing reads exactly
    that; a probe that skipped it would test a path no real trace takes.
    """
    trace_id = uuid.uuid4().hex
    now_ns = time.time_ns()
    batch = {
        "trace_id": trace_id,
        "spans": [
            {
                "trace_id": trace_id,
                "span_id": uuid.uuid4().hex[:16],
                "parent_span_id": None,
                "name": f"AgentRun:{agent_name}",
                "kind": "INTERNAL",
                "start_time": now_ns,
                "end_time": now_ns,
                "duration_ms": 0.0,
                "attributes": {
                    "agent.name": agent_name,
                    "service.name": "aisquare-cli",
                    "aisquare.probe": True,
                    "aisquare.probe.source": "aisquare doctor --live",
                },
            }
        ],
    }
    verdict = _request(
        f"{target.gateway_url}/v1/traces/ingest",
        api_key=target.api_key,
        body=batch,
        timeout=timeout,
    )
    # `_request` calls any 2xx ok, because `/ready` legitimately answers 200.
    # THIS probe's meaning is tied to one code: the docstring above, §5 of the
    # cutover runbook and the gateway contract all say 202. A 200 from a reverse
    # proxy, an auth portal or an API gateway's default route would otherwise
    # render as "test span accepted" — the word this row commits to — from an
    # endpoint that never saw a span. Measured before this narrowing: HTTP 200
    # produced `✓ … test span accepted as 'aisquare-planner' (HTTP 200)`.
    if verdict.ok and verdict.status != 202:
        return HttpVerdict(
            ok=False,
            status=verdict.status,
            detail=(
                f"HTTP {verdict.status} — the gateway answered but did not ACCEPT "
                "the span (ingest acknowledges with 202); check the URL reaches "
                "the gateway itself rather than a proxy in front of it"
            ),
            code=verdict.code,
            payload=verdict.payload,
        )
    return verdict


def register_roster(
    target: ResolvedTarget,
    agent_names: Sequence[str],
    *,
    timeout: float = _HTTP_TIMEOUT,
) -> HttpVerdict:
    """Declare this machine's agent identities against the workspace.

    Spans whose ``agent.name`` is unknown to the workspace are rejected, so
    this is the step that makes a fresh deployment able to receive anything at
    all. Idempotent: re-registering an existing name returns its publication id
    rather than minting a second one.
    """
    return _request(
        f"{target.gateway_url}/v1/agents/register-roster",
        api_key=target.api_key,
        body={"agents": list(agent_names)},
        timeout=timeout,
    )


def publication_ids(payload: Any) -> dict[str, str]:
    """Best-effort ``{agent name: publication id}`` from a roster response.

    The gateway forwards the Studio backend's body verbatim, so the shape is
    the backend's to change; walk it for the pair we need instead of pinning a
    schema we do not own, and let the caller print the raw body when this finds
    nothing.
    """
    found: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name = node.get("name") or node.get("agent_name") or node.get("agent")
            pub = node.get("publication_id") or node.get("publicationId")
            if isinstance(name, str) and pub is not None:
                found[name] = str(pub)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


# ── the doctor section ───────────────────────────────────────────────────────


def _ok(name: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.ok, detail=detail)


def _warn(name: str, detail: str, fix: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.warn, detail=detail, fix=fix)


def _fail(name: str, detail: str, fix: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.fail, detail=detail, fix=fix)


def checks(
    settings: ExplainabilitySettings | None = None,
    *,
    target_name: str | None = None,
    live: bool = False,
    env: Mapping[str, str] | None = None,
) -> list[DoctorCheck]:
    """The explainability section of ``aisquare doctor``, in dependency order.

    Severity follows a single rule: **an unwired machine is not a broken
    machine**. Nothing here fails while tracing is off — the section reads as
    guidance and names the one command that turns it on. Once an operator has
    switched it on, the same conditions become failures, because now they are
    the difference between traced and silently untraced.
    """
    try:
        resolved_settings = load_config().explainability if settings is None else settings
    except Exception as exc:  # diagnostics must never crash
        return [
            _warn(
                "explainability",
                f"could not read the config: {exc}",
                "Fix or reset it: aisquare init --reinit",
            )
        ]

    target = resolve_target(resolved_settings, target_name, env=env)
    on = resolved_settings.enabled
    switch = _check_switch(resolved_settings, target, live=live)

    # A machine that has never been pointed at a deployment gets ONE line —
    # the switch, carrying the command that starts the journey. Expanding to a
    # six-line section about a feature its owner never enabled is how the rest
    # of doctor's output stops being read. ``--live`` is an explicit request
    # for the full picture, so it always expands.
    touched = on or bool(resolved_settings.targets) or bool(target.gateway_url)
    if not (touched or live):
        return [switch]

    results = [switch, _check_sdk(on=touched, live=live, deployable=target.configured)]
    results.append(_check_config(target, on=on))
    results.append(_check_redaction(_redaction_level(), shipping=resolved_settings.ship))
    results.append(_check_proxy(target, on=on, live=live))
    if live:
        results.extend(_live_checks(target, on=on))
    return results


def _check_switch(
    settings: ExplainabilitySettings, target: ResolvedTarget, *, live: bool
) -> DoctorCheck:
    if not settings.enabled:
        # Never fail: a machine with tracing off is working as configured. The
        # severity is the one judgement call in this section — a permanent
        # yellow line on every install of a CLI whose owner never asked for
        # tracing is how a section gets ignored, so an untouched machine gets
        # the command in an ok line, and only a HALF-wired one (targets or a
        # gateway in the environment, switch still off) is worth a warning.
        if target.gateway_url or settings.targets:
            return _warn(
                "explainability",
                f"target '{target.name}' is configured but tracing is off — sessions "
                "run untraced and nothing reaches the gateway",
                "Turn it on: aisquare explainability enable",
            )
        return _ok(
            "explainability",
            "tracing is off (turn it on with: aisquare explainability enable)",
        )
    suffix = "" if live else " (add --live for the gateway round-trip)"
    return _ok(
        "explainability",
        f"tracing on, target '{target.name}' via {target.gateway_source}{suffix}",
    )


def _check_sdk(*, on: bool, live: bool, deployable: bool) -> DoctorCheck:
    presence = sdk_presence()
    name = "explainability sdk"
    if presence.shadowing:
        return _warn(
            name,
            "the 'aisquare' package root in this environment is not aisquare-cli's — "
            "the SDK ships a package of the same name and overwrote it",
            "Harmless for the CLI (it reads its version from dist metadata), but "
            "repair the root if you use the SDK's own facade: "
            "pip install --force-reinstall aisquare",
        )
    if not presence.present:
        detail = (
            "SDK not installed — the proxy lane still traces model traffic, but the "
            "CLI cannot ship its own insights as spans"
        )
        if not on:
            return _ok(name, f"{detail} (install: {INSTALL_HINT})")
        return _warn(name, detail, f"Install it: {INSTALL_HINT}")
    if deployable and not presence.importable:
        # `present` is an OR — importable or a console script on PATH — but the
        # CLIENT lane needs the import: `sdk_available()` is `find_spec(...)`
        # alone. So this row read green on a machine whose client lane cannot
        # run, which is clause 2 of the north star failing in silence.
        #
        # Gated on `target.configured` (a gateway AND a key), NOT on
        # `settings.ship`. Measured: `ship` can never be True in this state —
        # `shipping_offer()` refuses with "extra not installed" when the SDK is
        # not importable, and `configure_shipping` sits behind that refusal. A
        # `shipping and not importable` gate is unreachable in production and
        # would only ever pass in a test that built the impossible state by
        # hand. `configured` is the reachable predicate: it is exactly the
        # machine that WOULD ship, and is the one silently not shipping.
        return _warn(
            name,
            f"{f'SDK {presence.version}' if presence.version else 'the SDK'} is "
            f"reachable only as a console script ({presence.script}) and cannot "
            "be imported here — the proxy "
            "lane still traces model traffic, but the client lane is OFF: the "
            "CLI cannot ship its own insights as spans, and 'init "
            "--explainability' will decline to turn shipping on",
            f"Install the SDK into the same environment as aisquare: {install_hint()}",
        )
    where = "console script" if presence.script else "importable"
    detail = f"SDK {presence.version or 'present'} ({where})"
    if not live:
        detail += " — its own checks run under --live"
    return _ok(name, detail)


def _check_config(target: ResolvedTarget, *, on: bool) -> DoctorCheck:
    name = "explainability config"
    degrade: Degrade = _fail if on else _warn
    if not target.gateway_url:
        return degrade(
            name,
            f"target '{target.name}' has no gateway URL",
            "Point it at a deployment: aisquare explainability enable "
            f"--target {target.name} --gateway-url <url>",
        )
    if not target.api_key:
        return degrade(
            name,
            f"target '{target.name}' -> {target.gateway_url} ({target.gateway_source}), "
            f"but ${target.api_key_env} is not set in this shell",
            f"Export the workspace key as ${target.api_key_env} (the CLI reads it from "
            "the environment and never stores it), or point the target at another "
            "variable: aisquare explainability enable --target "
            f"{target.name} --key-env <VAR>",
        )
    identities = ", ".join(target.agent_names) or "none"
    return _ok(
        name,
        f"target '{target.name}' -> {target.gateway_url} ({target.gateway_source}), "
        f"key from ${target.api_key_env}, identities: {identities}",
    )


#: One sentence per level, written for someone about to point this at prod.
#: Each says what LEAVES and, where it matters, what does not — the two are
#: easy to blur and expensive to blur, because a reader who thinks their local
#: history is scrubbed goes hunting for prompts that are sitting right there.
_REDACTION_SUMMARY = {
    RedactionLevel.off: (
        "off — insights leave this machine exactly as typed; local capture is unchanged"
    ),
    RedactionLevel.standard: (
        "standard — credentials are removed from insights leaving this machine "
        "(paths and hostnames are kept); local capture keeps what you typed"
    ),
    RedactionLevel.strict: (
        "strict — credentials plus identity (emails, home paths) are removed from "
        "insights leaving this machine; local capture keeps what you typed"
    ),
}


def _redaction_level() -> RedactionLevel:
    """The configured level, defaulting rather than raising — doctor must not crash."""
    try:
        return load_config().redaction.level
    except Exception:
        return RedactionLevel.standard


def redaction_summary(level: RedactionLevel) -> str:
    """What the active redaction level means, in one line, for status and doctor.

    ONE string for both surfaces so they cannot drift into saying different
    things about the same setting — which is the failure mode that made this
    setting untrustworthy in the first place.
    """
    return _REDACTION_SUMMARY.get(level, f"{level} — unrecognised level, nothing is guaranteed")


def _check_redaction(level: RedactionLevel, *, shipping: bool) -> DoctorCheck:
    """State the level. Never a failure: it is a setting, not a health condition.

    ``off`` is somebody's decision and doctor does not overrule decisions — it
    makes them visible. The severity that WOULD be wrong here is `fail`, which
    reads as "your machine is broken" for a machine doing exactly what it was
    told. What an operator needs at 08:00 is the sentence, not the colour.
    """
    detail = redaction_summary(level)
    if not shipping:
        detail += " (nothing is being shipped yet)"
    return _ok("explainability redaction", detail)


def _proxy_source(settings: ExplainabilitySettings, target: ExplainabilityTarget) -> str:
    """Whether anyone CHOSE this proxy URL, or it is the untouched default.

    Compared against the model's declared default rather than a literal, so the
    two cannot drift apart the day someone edits the config schema.
    """
    if target.proxy_url:
        return "config"
    default = ExplainabilitySettings.model_fields["proxy_url"].default
    return "config" if settings.proxy_url != default else "default"


@dataclass(frozen=True)
class ProxyState:
    """What to say about the tracing proxy, and whether it is a problem.

    ONE description for ``status`` and ``doctor``, because they were already
    drifting: doctor knew to stay quiet while tracing was off and status did
    not, so a cold machine read green in one surface and broken in the other.
    """

    summary: str
    healthy: bool
    problem: bool
    remediation: str = ""


#: Remediation for a proxy that was configured and is not answering. Shared so
#: the two surfaces cannot offer different advice for the same state.
_PROXY_FIX = (
    "Start the SDK's claude_code proxy, or point explainability.proxy_url at a "
    "running one: aisquare explainability enable --proxy-url <url>"
)


def proxy_state(
    target: ResolvedTarget,
    *,
    on: bool,
    live: bool = False,
    prober: Callable[[str], ProxyProbe] | None = None,
) -> ProxyState:
    """Describe the proxy lane truthfully for a machine in ANY of its states.

    Three states, and only the last is red:

    * nobody configured a proxy and tracing is off — the shipped default points
      at loopback and nothing is listening, which is CORRECT for an install
      that has never asked for tracing. Saying "unreachable" here sends an
      operator to debug a proxy that was never meant to exist yet. Not probed
      at all: dialling an address the operator never chose spends their time on
      a question nobody asked.
    * a proxy is configured but tracing is off — nothing is being traced, so
      nothing can be broken. Informational.
    * tracing is ON and the proxy does not answer — genuinely red: launches
      still succeed (they never block on this) but they go UNTRACED, silently,
      which is the whole failure this lane exists to prevent.
    """
    # Resolved here rather than bound as a default argument, so a test (or a
    # caller) can substitute a prober by patching this module.
    ask = prober or probe_proxy
    if not on:
        if target.proxy_source == "default":
            # Never dialled, --live or not: nobody asked about this address.
            return ProxyState(
                summary=(
                    f"not configured — the default {target.proxy_url} is not consulted "
                    "while tracing is off"
                ),
                healthy=False,
                problem=False,
            )
        if not live:
            return ProxyState(
                summary=f"not consulted while tracing is off ({target.proxy_url})",
                healthy=False,
                problem=False,
            )
        # --live means "make the calls", and this is the one an operator
        # mid-cutover actually wants: they started a proxy and want to know it
        # answers BEFORE flipping tracing on. Still never a failure — nothing
        # is being traced, so nothing is broken either way — but the answer
        # carries what it means, because a fact without its consequence is a
        # fact to guess at.
        verdict = ask(target.proxy_url)
        if verdict.healthy:
            return ProxyState(
                summary=(
                    f"answered at {target.proxy_url}, but tracing is off — nothing is "
                    "being traced yet (turn it on: aisquare explainability enable)"
                ),
                healthy=True,
                problem=False,
            )
        return ProxyState(
            summary=(
                f"{verdict.reason} — nothing is untraced yet because tracing is off, "
                "but it will be the moment you enable it"
            ),
            healthy=False,
            problem=False,
            remediation=_PROXY_FIX,
        )
    verdict = ask(target.proxy_url)
    if verdict.healthy:
        return ProxyState(
            summary=f"claude_code proxy healthy at {target.proxy_url}",
            healthy=True,
            problem=False,
        )
    return ProxyState(
        summary=f"{verdict.reason} — sessions launch UNTRACED (they never block on this)",
        healthy=False,
        problem=True,
        remediation=_PROXY_FIX,
    )


def _check_proxy(target: ResolvedTarget, *, on: bool, live: bool = False) -> DoctorCheck:
    """The session-tracing lane: is the local proxy the one we expect?

    Loopback and 1.5s worst case, so it stays inside the "doctor does not make
    network calls" spirit — and when tracing is off it is not consulted at all.
    The sentence comes from :func:`proxy_state`, which ``status`` also renders,
    so the two surfaces cannot describe one machine differently.
    """
    name = "explainability proxy"
    state = proxy_state(target, on=on, live=live)
    if state.problem:
        return _fail(name, state.summary, state.remediation)
    return _ok(name, state.summary)


def _live_checks(target: ResolvedTarget, *, on: bool) -> list[DoctorCheck]:
    """Gateway-side checks: reachability, then a real span round-trip."""
    degrade: Degrade = _fail if on else _warn
    if not target.configured:
        return [
            _warn(
                "explainability gateway",
                "skipped — no gateway URL and key for this target",
                "Configure the target first: aisquare explainability enable "
                f"--target {target.name} --gateway-url <url>",
            )
        ]

    results: list[DoctorCheck] = []
    ready = probe_ready(target.gateway_url)
    if not ready.ok:
        results.append(
            degrade(
                "explainability gateway",
                f"{target.gateway_url}/ready — {ready.detail}",
                "Check the URL and your network path to it; if it is a private "
                "deployment, confirm the VPN or tunnel is up",
            )
        )
        # The ingest probe stays gated — posting a span to a URL that just
        # failed readiness would produce a second confusing failure, and could
        # post to something that is not our gateway. But an ABSENT row reads
        # exactly like a passing one: an operator with a red gateway cannot
        # tell whether ingest is also broken or merely unasked, which is
        # whether fixing the URL is the whole job. Say it was skipped, in the
        # idiom this function already uses twice.
        results.append(
            _warn(
                "explainability ingest",
                "skipped — the gateway did not answer /ready, so no span was posted; "
                "this is not a verdict on ingest",
                "Fix the gateway row above, then re-run: aisquare doctor --live",
            )
        )
        # `_sdk_checks` asks the SDK's own doctor and never touches the gateway,
        # so a gateway failure was silently removing rows that had nothing to do
        # with it.
        results.extend(_sdk_checks(degrade=degrade))
        return results
    results.append(_ok("explainability gateway", f"{target.gateway_url}/ready — HTTP 200"))

    identity = target.agent_names[0] if target.agent_names else None
    if identity is None:
        results.append(
            _warn(
                "explainability ingest",
                "skipped — the identity template renders no agent names",
                "Fix explainability.agent_name_template (it must contain {role})",
            )
        )
        results.extend(
            _sdk_checks(degrade=degrade)
        )  # local rows; nothing here depends on the gateway
        return results

    verdict = probe_ingest(target, identity)
    results.append(_ingest_check(verdict, identity, degrade=degrade))
    if verdict.ok:
        results.append(
            _warn(
                "explainability governance",
                "traces land, but runs stay UNGOVERNED until a rule book is attached "
                "to the studio (an ingest key cannot verify this from here)",
                "Attach a rule book to the studio in the dashboard, then re-run "
                "aisquare doctor --live",
            )
        )
    results.extend(_sdk_checks(degrade=degrade))
    return results


#: ``_fail`` when the operator has switched tracing on (broken now means
#: silently untraced), ``_warn`` when they have not (nothing is promised yet).
Degrade = Callable[[str, str, str], DoctorCheck]


def _billing_band(payload: object) -> str | None:
    """The gateway's low-credit band from an ingest 202, when it sent one.

    Tolerant by construction: any payload shape that is not a mapping with a
    non-empty string here means "no band", because a diagnostic must never turn
    a successful ingest into an error over a field it failed to parse.
    """
    if not isinstance(payload, dict):
        return None
    band = payload.get("billing_warning")
    return band if isinstance(band, str) and band else None


def _ingest_check(verdict: HttpVerdict, identity: str, *, degrade: Degrade) -> DoctorCheck:
    name = "explainability ingest"
    if verdict.ok:
        accepted = f"test span accepted as '{identity}' (HTTP {verdict.status})"
        # The gateway puts a low-credit signal ON the 202 rather than failing:
        # `IngestResponse.billing_warning` is "a BAND STRING ONLY (warn|hard),
        # never a numeric balance", and a hard band only becomes a 402 when the
        # deployment sets BILLING_ENFORCEMENT_MODE=hard. THE DEFAULT IS soft, so
        # an exhausted workspace answers 202 and says so in the body. Dropping
        # it renders "accepted" over a studio that is out of credit.
        band = _billing_band(verdict.payload)
        if band is not None:
            return _warn(
                name,
                f"{accepted} — but the workspace credit balance is in the "
                f"'{band}' band, which the gateway reported on the 202",
                "Top up the studio's credits; ingest still lands today, and "
                "stops if the deployment enforces the hard band",
            )
        return _ok(name, accepted)
    if verdict.status in (401, 403):
        return degrade(
            name,
            f"the gateway rejected the key ({verdict.detail})",
            "Check that the exported key is this deployment's WORKSPACE key and has "
            "not been rotated; a studio-scoped key cannot ingest",
        )
    if verdict.code in ("agent_not_registered", "no_agent_identity") or verdict.status == 409:
        return degrade(
            name,
            f"'{identity}' is not a registered identity in this workspace "
            f"({verdict.code or verdict.detail})",
            "Register this machine's roster: aisquare explainability register",
        )
    if verdict.status == 429:
        return _warn(
            name,
            f"rate limited ({verdict.detail}) — the key works, the gateway is busy",
            "Re-run in a minute; if it persists, reduce ingest frequency",
        )
    return degrade(
        name,
        f"test span not accepted: {verdict.detail}",
        "Re-run with the gateway logs open; the status and body above are the gateway's own words",
    )


def _sdk_checks(*, degrade: Degrade) -> list[DoctorCheck]:
    """The SDK doctor's rows, folded in as ``sdk:<name>`` checks.

    Reused rather than reimplemented — but filtered: the Agno adapter and the
    gateway's OPENAI_API_KEY are not this integration's business, and reporting
    them red would train an operator to ignore the section.

    ``degrade`` is the same callable every first-party row here goes through:
    ``_fail`` when tracing is on, ``_warn`` when it is off. These rows used to
    call ``_fail`` directly, so with tracing OFF an unreachable gateway read
    ``⚠ explainability gateway`` beside ``✗ sdk:gateway_ready`` — one condition,
    two verdicts, and the louder one took the exit code with it. An observer
    that fails your build is not an observer.
    """
    results: list[DoctorCheck] = []
    for row_name, status, detail in sdk_doctor():
        if row_name in _SDK_NOISE:
            continue
        name = f"sdk:{row_name}"
        if status in ("ok", "pass"):
            results.append(_ok(name, detail))
        elif status in ("warning", "warn", "missing"):
            results.append(_warn(name, detail, "See the SDK's own guidance above"))
        else:
            results.append(
                degrade(name, detail, f"Re-run the SDK's own doctor for detail: {_SDK_SCRIPT}")
            )
    return results


# ── --fix ────────────────────────────────────────────────────────────────────


def apply_fixes(
    *,
    target: str | None = None,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
) -> list[str]:
    """Repair what a machine can repair by itself, and report each action.

    Deliberately narrow. Turning the switch on and installing the SDK are
    reversible, local, and exactly what the operator asked for by typing
    ``--fix``. Inventing a gateway URL or a key is not: those are the operator's
    to supply, so a missing one stays a red line with the command that sets it
    rather than a guess this function makes on their behalf.

    Installing reaches the network and mutates the environment the CLI itself
    runs in, so it needs consent every time — ``assume_yes`` or a ``confirm``
    callback that says yes. Without either, it is reported as skipped.
    """
    actions: list[str] = []
    try:
        config = load_config()
    except Exception as exc:  # never crash the doctor we are called from
        return [f"could not read the config ({exc}) — fix it first: aisquare init --reinit"]

    if not config.explainability.enabled:
        config.explainability.enabled = True
        if target:
            config.explainability.target = target
        try:
            save_config(config)
            actions.append("enabled explainability tracing for this machine")
        except Exception as exc:
            actions.append(f"could not write the config ({exc})")

    presence = sdk_presence()
    if not presence.present:
        allowed = assume_yes or (
            confirm is not None
            and confirm(
                f"Install the explainability SDK into {sys.executable}? "
                "It shares the 'aisquare' import package with this CLI"
            )
        )
        if not allowed:
            actions.append(f"SDK install skipped — run it yourself: {INSTALL_HINT}")
        else:
            ok, detail = install_sdk()
            actions.append(detail if ok else f"SDK install failed: {detail}")
    return actions


def install_sdk() -> tuple[bool, str]:
    """Install the SDK into *this* interpreter's environment.

    Returns ``(ok, detail)``; never raises. The caller is expected to have
    obtained consent first — this reaches the network and mutates the
    environment the CLI itself runs in, which for a pipx install is the CLI's
    own venv.
    """
    argv = [sys.executable, "-m", "pip", "install", f"{_SDK_DIST}[explainability]"]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{' '.join(argv)} failed to start: {exc}"
    if completed.returncode != 0:
        return False, _summarise(completed.stderr or completed.stdout, limit=400)
    return True, f"installed {_SDK_DIST}[explainability]"
