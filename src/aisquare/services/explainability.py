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

THE CORRELATION SPINE. ``X-Pipeline-Id`` is only useful if it is the SAME id
the board knows the session by — otherwise the two datasets (board tasks,
claims and notes on one side, gateway Runs on the other) cannot be joined at
all. The board's key is the agent's own session id, which the agent reports to
the ``SessionStart`` hook, so the only place the two can be made equal is the
launcher: it mints the id, hands it to the agent as ``--session-id``, and
traces under that same id. ``plan_session_identity`` decides whether that is
possible for a given launch, and ``record_join`` writes down the result.

The join log is JSON Lines at ``~/.aisquare/explainability/joins.jsonl``
(``AISQUARE_HOME`` relocates it), one object per traced launch::

    {"started_at": "2026-08-17T02:31:07+00:00",  # UTC, ISO 8601
     "session_id": "5f1c…",   # the agent's session id == the board row id;
                              # null when this launch could not be joined
     "agent_name": "aisquare-coder",   # X-Agent-Name, the studio identity
     "pipeline_id": "5f1c…",  # X-Pipeline-Id, the gateway Run key
     "joined": true,          # session_id == pipeline_id, i.e. joinable
     "role": "coder",
     "cwd": "/home/me/repo"}

Append-only and never read by the launcher, so it cannot slow a launch down or
grow into shared state; ``joined: false`` rows are kept deliberately, because
"this Run has no board row and here is why" is the fact an operator needs.

THE BOUNDARY. Because both variables live in the PROCESS environment, the unit
of attribution is the process — a Run is a process, not an agent. An in-process
agent (a Claude Code Task subagent, a Workflow step) inherits that environment
verbatim because it *is* the same process, so it cannot carry a different
identity and the proxy correctly records one Run. Per-role and per-session
numbers are real; per-subagent numbers do not exist, and a query that appears
to return one is reading root-level spans and attributing them to whichever
subagent the reader assumed. Whoever designs an experiment on this data needs
that before they design it, so it is written for them in
``docs/explainability-tracing-boundary.md`` rather than only here.

Everything here fails open by design. Tracing is an observer — the moment it
would break a launch (proxy down, wrong proxy mode, an ANTHROPIC_* var the
user already owns, a config typo, an unwritable join log) we launch the
session untraced and say why, we never block. The only hard opt-in is
``explainability.enabled`` in ``~/.aisquare/config.toml``, which defaults to
off.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from aisquare.core import insights, outbox, paths
from aisquare.core.config import ExplainabilitySettings, load_config, save_config
from aisquare.core.store import store_session

#: Vars the wiring wants to set. If the user's environment already defines one
#: they are routing Anthropic traffic deliberately (custom gateway, another
#: observability layer) — clobbering it would silently redirect or untrace
#: THEIR setup, so we stand down instead.
RESERVED_ENV_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS")

#: The proxy /health contract this wiring was verified against: any service or
#: mode mismatch means "some other listener owns that port" (e.g. a creator-mode
#: proxy), and pointing a Claude Code session at it is not a risk worth taking.
_EXPECTED_SERVICE = "aisquare-proxy"
_EXPECTED_MODE = "claude_code"

#: Role names travel inside an HTTP header value; anything beyond this set is
#: either a typo or an injection attempt, and both fail open.
_SAFE_ROLE = re.compile(r"^[A-Za-z0-9._-]+$")

_PROBE_TIMEOUT_SECONDS = 1.5

#: The ONE program verified to accept ``--session-id <uuid>`` (Claude Code
#: 2.1.233). Matched on the basename, so an absolute path counts and nothing
#: else does — not ``claude2``, not a wrapper merely NAMED after claude. Since
#: #57 a role can be bound to any executable, and an unknown flag kills the
#: launch; every other binary joins through the hook seam instead, which needs
#: no flag at all, so the narrow match costs nothing but a nicety.
_SESSION_ID_AGENT = "claude"

#: Flags whose presence means the caller (or the human) already owns the
#: session id, so we must read it rather than choose one.
_SESSION_ID_FLAG = "--session-id"
_RESUME_FLAGS = ("--resume", "-r")
_CONTINUE_FLAGS = ("--continue", "-c")

#: Escape hatch for the launch that pinning breaks. Set to 0/false/no/off to
#: keep the launcher out of the agent's argv; sessions then trace unjoined
#: instead of not at all.
_PIN_ENV_VAR = "AISQUARE_PIN_SESSION_ID"
_OFF_VALUES = {"0", "false", "no", "off"}

#: The run key the launcher chose, exported alongside the header pair by
#: ``aisquare launch``, ``team spawn --exec`` and ``explainability env``. Two
#: jobs, and both matter: a shell command can pass the same id to the agent it
#: is about to run, and its PRESENCE marks the ``ANTHROPIC_*`` beside it as
#: OURS. Nothing else sets it, so "marker present" is the only reliable way to
#: tell our own wiring apart from a gateway the operator exported themselves —
#: which is what makes disowning safe.
#:
#: Named for what it holds. It was ``AISQUARE_SESSION_ID``, and that name is
#: what let a careful reader key spans on it as though it were the board's
#: session id — which it is NOT on any launch we could not pin, so those spans
#: opened a second Run beside the model traffic. The value is the pipeline id;
#: the name now says so.
PIPELINE_ID_ENV_VAR = "AISQUARE_PIPELINE_ID"

#: The identity our wiring ran under, carried beside the run key so a process
#: downstream of the launcher can record the join without re-reading config.
#:
#: Deliberately NOT ``AISQUARE_AGENT_NAME``. That name is already spoken for —
#: see :data:`AGENT_NAME_ENV_VAR` below, the routing identity the SDK reads and
#: the operator sets in their env file. Writing it from the launcher would
#: silently override the operator's routing, which is the exact thing the
#: reserved-var guard exists to prevent for ``ANTHROPIC_*``. A marker is
#: internal plumbing and has no business sharing a name with a public contract.
TRACE_AGENT_NAME_ENV_VAR = "AISQUARE_TRACE_AGENT_NAME"


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


@dataclass(frozen=True)
class SessionIdentity:
    """Which id keys this Run, and what the agent must be told to make it true.

    ``session_id`` is the id the board will know the session by — pass it to
    ``wire_session`` so the Run and the board row share a key. ``None`` means
    this launch cannot be joined (the agent picks its own id); the trace is
    still worth taking, so callers wire it anyway and surface ``note``.
    ``inject_args`` is appended to the agent's argv, and is empty whenever the
    id came from the caller rather than from us.
    """

    session_id: str | None
    inject_args: tuple[str, ...] = ()
    note: str = ""


def _flag_value(args: Sequence[str], flag: str) -> tuple[bool, str | None]:
    """``(flag is present, its value)`` for ``--flag value`` and ``--flag=value``.

    A next token that starts with ``-`` is another flag, not a value — that is
    how ``--resume`` reads when it means "show me the picker".
    """
    for position, arg in enumerate(args):
        if arg == flag:
            following = args[position + 1] if position + 1 < len(args) else None
            return True, following if following and not following.startswith("-") else None
        if arg.startswith(f"{flag}="):
            return True, arg.split("=", 1)[1] or None
    return False, None


def disown_inherited_trace(env: MutableMapping[str, str]) -> str | None:
    """Drop OUR tracing identity from ``env``; return the run it belonged to.

    A traced agent's environment carries the wiring that traced it, and every
    process it starts inherits that. For an ordinary child (a probe, a git
    call) the answer is to strip it — see ``core.spawn``. For a child that is
    itself an agent the answer is different: it should be traced, just not as
    its parent. Leaving the inherited pair in place gives it the parent's
    ``X-Pipeline-Id`` and merges two sessions into one Run; leaving it in place
    *and* standing down on the reserved-var guard — what happened before this
    existed — drops every agent below the first off the trace entirely.

    So the parent's identity is removed and the caller wires a fresh one. Only
    ever OUR identity: without :data:`PIPELINE_ID_ENV_VAR` beside them the
    ``ANTHROPIC_*`` are a gateway the operator set up, and those are theirs to
    keep — the caller then stands down exactly as it always did.

    ``None`` when there was nothing of ours to disown, which is every ordinary
    launch.
    """
    parent_run = (env.get(PIPELINE_ID_ENV_VAR) or "").strip()
    if not parent_run or not any(env.get(name) for name in RESERVED_ENV_VARS):
        return None
    for name in (*RESERVED_ENV_VARS, PIPELINE_ID_ENV_VAR, TRACE_AGENT_NAME_ENV_VAR):
        env.pop(name, None)
    return parent_run


def trace_marker(wiring: SessionWiring) -> dict[str, str]:
    """The env a traced agent carries beyond the headers themselves.

    Two consumers. A spawn command run from inside the agent reads it to tell
    our wiring apart from the operator's own gateway, and the hook running
    inside the agent reads it to record the session→Run join — the only place
    that knows BOTH the pipeline id (from here) and the board session id (from
    Claude Code). Empty for an untraced launch: a stale marker would have the
    agent's hook write down a join that is not true, which is worse than no
    record because it reads as evidence.
    """
    if not wiring.traced or not wiring.pipeline_id:
        return {}
    marker = {PIPELINE_ID_ENV_VAR: wiring.pipeline_id}
    if wiring.agent_name:
        marker[TRACE_AGENT_NAME_ENV_VAR] = wiring.agent_name
    return marker


def traced_by(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """``(pipeline_id, agent_name)`` this process was launched to trace.

    ``None`` when it was not — which is every ordinary session, so a caller on
    the hook path leaves after one lookup.
    """
    source = os.environ if env is None else env
    pipeline_id = (source.get(PIPELINE_ID_ENV_VAR) or "").strip()
    if not pipeline_id:
        return None
    return pipeline_id, (source.get(TRACE_AGENT_NAME_ENV_VAR) or "").strip()


def accepts_session_id(binary: str) -> bool:
    """Whether ``binary`` is one we may hand ``--session-id`` to."""
    if os.environ.get(_PIN_ENV_VAR, "").strip().lower() in _OFF_VALUES:
        return False
    return os.path.basename(binary) == _SESSION_ID_AGENT


def plan_session_identity(binary: str, args: Sequence[str]) -> SessionIdentity:
    """Decide the id this launch is traced under, before anything is launched.

    Four cases, in order. The caller already named a session id (``--session-id``)
    or a session to resume: read it, inject nothing. The caller asked for a
    session we cannot name in advance (``--continue``, a bare ``--resume``):
    no join is possible, and *guessing* an id would be worse than none — two
    agents on one id merge into a single board row and a single Run. The agent
    may not speak the flag: same answer, for the same reason. Otherwise mint a
    fresh id and hand it over — the only case that produces a real join.
    """
    present, value = _flag_value(args, _SESSION_ID_FLAG)
    if present:
        if value is None:
            return SessionIdentity(
                None, note=f"{_SESSION_ID_FLAG} was passed without a readable value"
            )
        return SessionIdentity(value)
    for flag in _RESUME_FLAGS:
        present, value = _flag_value(args, flag)
        if present:
            if value is not None:
                return SessionIdentity(value)
            return SessionIdentity(None, note=f"{flag} picks the session at run time")
    for flag in _CONTINUE_FLAGS:
        if flag in args:
            return SessionIdentity(None, note=f"{flag} resumes a session chosen at run time")
    if not accepts_session_id(binary):
        return SessionIdentity(
            None, note=f"{os.path.basename(binary)!r} is not {_SESSION_ID_AGENT}"
        )
    session_id = str(uuid.uuid4())
    return SessionIdentity(session_id, inject_args=(_SESSION_ID_FLAG, session_id))


def record_join(
    *,
    session_id: str,
    pipeline_id: str,
    agent_name: str = "",
    role: str | None = None,
    cwd: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Append one board-session-to-Run mapping; return why it could not be.

    Written from the hook running INSIDE the agent, which is the only place
    that holds both halves: Claude Code hands it the real session id — the id
    the board row uses — and the launcher left the pipeline id in the
    environment. No flag and no binary-specific knowledge, so a role bound to
    a wrapper joins exactly like the default agent does.

    Returns ``None`` on success and a reason otherwise — never raises. Rows are
    observations, not state: a session seen twice (a ``/clear``, a resume)
    appends twice, and readers dedupe on ``(session_id, pipeline_id)``. That
    keeps this a single append with nothing to read first.
    """
    record = {
        "started_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "session_id": session_id,
        "pipeline_id": pipeline_id,
        "agent_name": agent_name,
        "role": role,
        "cwd": cwd if cwd is not None else os.getcwd(),
    }
    path = paths.explainability_joins_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError as exc:
        return f"join record not written ({exc})"
    return None


def join_records(path: Path | None = None) -> list[dict[str, object]]:
    """Every readable line of the join log, oldest first.

    Unreadable file or half-written line → skipped, not raised: this is a log
    of observations, and one bad line must not hide the rest.
    """
    target = path or paths.explainability_joins_path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _usable_base_url(value: str) -> bool:
    """Whether ``value`` is something an agent can actually use as a base URL.

    Deliberately shallow — a scheme the agent speaks and a host to speak it to,
    and nothing about whether anyone is listening, which is the probe's job.
    It exists for one reason: to stop a value the AGENT cannot parse from
    reaching its environment, because that failure mode is not a lost trace,
    it is a dead session.
    """
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


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
    prober: Callable[[str], ProxyProbe] | None = None,
) -> SessionWiring:
    """Build the env delta that traces one session, or explain why not.

    ``session_id`` becomes the run's ``X-Pipeline-Id`` when given — pass the
    agent session id so board rows and dashboard Runs share a key; otherwise a
    fresh UUID keeps concurrent sessions from merging into one Run. ``base_env``
    is consulted (never mutated) for vars the user already owns.

    ``prober`` resolves HERE rather than as a default argument. A default binds
    the function object at def time, so patching ``probe_proxy`` on this module
    could not reach it — and all three production callers pass nothing, so a
    test driving one of them through the CLI would have got the real network
    prober and passed by agreeing with reality instead of verifying anything.
    Passing ``prober=`` explicitly still wins.
    """
    ask = prober or probe_proxy
    if not settings.enabled:
        return SessionWiring(traced=False, reason="explainability is disabled (config default)")

    if not _SAFE_ROLE.match(role or ""):
        return SessionWiring(
            traced=False, reason=f"role {role!r} is not header-safe — launching untraced"
        )

    # The one value here that can cost a LAUNCH rather than a trace. The agent
    # parses ANTHROPIC_BASE_URL before it can report anything, so a malformed
    # one does not degrade to untraced — it dies at its first request with
    # "API Error: Invalid URL" and exit 1, before a byte reaches the proxy.
    # Checked ahead of the probe, whose "unreachable" would blame the network
    # for what is a typo in config. Refused, never repaired: a value we
    # invented is a value nobody configured, and the operator would never
    # learn theirs was wrong.
    if not _usable_base_url(settings.proxy_url):
        return SessionWiring(
            traced=False,
            reason=f"explainability.proxy_url {settings.proxy_url!r} is not an "
            "http(s) URL — launching untraced",
        )

    # Ordered AFTER the value check on purpose: this one is about the
    # operator's routing, and we judge only what WE would set. A base_env the
    # user owns makes us stand down whatever it contains — policing their URL
    # is not ours to do.
    if base_env:
        taken = [name for name in RESERVED_ENV_VARS if base_env.get(name)]
        if taken:
            reason = (
                f"{' and '.join(taken)} already set — not overriding your "
                "routing, launching untraced"
            )
            # Standing down is not the same as saying nothing. If the value we
            # are deferring to cannot be parsed as a URL, this launch is about
            # to die with "API Error: Invalid URL" and exit 1 — and the operator
            # will have no idea why, because the failure surfaces from the agent
            # long after the shell that set it. Naming it is the ONLY thing we
            # can do here that is not overriding their routing.
            ambient = base_env.get("ANTHROPIC_BASE_URL")
            if ambient and not _usable_base_url(ambient):
                reason += (
                    f" — WARNING: {ambient!r} is not an http(s) URL, so the agent "
                    "will fail to start; unset or correct it"
                )
            return SessionWiring(traced=False, reason=reason)

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

    verdict = ask(settings.proxy_url)
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

#: ...and what to say instead on an EDITABLE install, where that command does
#: not merge the two packages, it SHADOWS ours: the SDK's real `aisquare/`
#: package in site-packages wins wholesale and every command dies at import.
#: Measured on BOTH editable shapes pip produces — the `.pth` line that appends
#: the checkout's src/ to sys.path, and the import-hook form
#: (`_editable_impl_*.py`, hatchling's `dev-mode-exact`) — and both are bricked
#: identically, so this is a property of editable installs rather than of one
#: packaging style. Also measured: reinstalling editable does NOT recover it,
#: only uninstalling the SDK does, and a non-editable install is unaffected.
#:
#: This is the only moment the warning can be delivered. Once the extra is in,
#: the CLI cannot start, so no check of ours will ever run to explain it.
EDITABLE_INSTALL_HINT = (
    "this is an editable checkout — installing the extra here shadows it and "
    "every command dies with \"No module named 'aisquare.cli'\". Install the "
    "extra in a separate (non-editable) environment; if you already did, "
    "recover with: pip uninstall aisquare"
)


def _package_root() -> str:
    """Where this package is imported from. A seam so tests can state a location."""
    import aisquare.core

    return str(aisquare.core.__file__ or "")


def running_editable() -> bool:
    """Whether this CLI runs from a checkout rather than from site-packages.

    Never raises: this only ever decides which sentence to print.
    """
    try:
        root = _package_root()
    except Exception:
        return False
    return bool(root) and not any(part in root for part in ("site-packages", "dist-packages"))


def install_hint(*, editable: bool | None = None) -> str:
    """The advice to print for THIS install."""
    is_editable = running_editable() if editable is None else editable
    return EDITABLE_INSTALL_HINT if is_editable else INSTALL_HINT


def key_path() -> Path:
    """File holding the workspace ingest key (mode 600)."""
    return paths.aisquare_home() / "explainability-key"


def _stored_api_key() -> str | None:
    """The machine-local key file, with no environment fallback.

    Deliberately narrower than :func:`resolve_api_key`: that one also reads
    ``EXPLAINABILITY_API_KEY``, which is correct when the target names that
    variable and WRONG when it names another. A staging key must not satisfy a
    prod target just because it happens to be in the shell.
    """
    try:
        stored = key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return stored or None


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


def _active_deployment(target_name: str | None = None) -> tuple[str, str, str | None]:
    """Where the client lane ships, and with which key — from the ACTIVE TARGET.

    Returns ``(gateway_url, key_env_name, key)``.

    Both lanes must name one deployment. Reading the top-level ``gateway_url``
    and a hardcoded ``EXPLAINABILITY_API_KEY`` here is how the two came apart:
    an operator who configured shipping under a staging shell and then ran
    ``enable --target prod`` moved the proxy lane and not this one, so model
    traffic went to prod while insights kept going to staging — and ``status``
    printed the prod gateway, because the line a human reads resolves the
    target. Both halves looked healthy. Nobody was told.

    ``resolve_target`` falls back to the top-level values when no target was
    ever created, so the single-deployment machine ``init --explainability``
    produces is unaffected.

    Imported inside the function because ``explainability_ops`` imports this
    module for ``probe_proxy``; at module scope that is a cycle.
    """
    from aisquare.services.explainability_ops import resolve_target

    settings = load_config().explainability
    target = resolve_target(settings, target_name)
    # `resolve_target` looks at the target and the SDK's env var; the top-level
    # `gateway_url` is what `configure_shipping` writes on a machine that never
    # made a target, so it is the last fallback rather than an alternative.
    gateway_url = target.gateway_url or settings.gateway_url
    # The key comes from the variable the TARGET names. The machine-local key
    # file answers ONLY when no variable was named — it holds one unlabelled
    # key, so the moment a deployment declares "my key lives in $PROD_KEY" the
    # file cannot stand in for it. Reproduced before this guard existed: follow
    # the CLI's own "or write <key file>" advice while on staging, switch to
    # prod, and the STAGING key went to the PROD gateway. The reverse is worse
    # — a prod key disclosed to a staging host.
    key = target.api_key
    if key is None and target.api_key_env == KEY_ENV_VAR:
        key = _stored_api_key()
    return gateway_url, target.api_key_env, key


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


def shipping_state(target_name: str | None = None) -> ShippingState:
    """Describe the client lane as it stands right now, for the ACTIVE target."""
    settings = load_config().explainability
    gateway_url, key_env, target_key = _active_deployment(target_name)
    # The target's named variable first; the key FILE stays a fallback for the
    # single-deployment setup that `init --explainability` writes.
    has_key = target_key is not None
    sdk = sdk_available()
    counts = outbox.counts()
    # Every "on" state names the DESTINATION. Counts alone cannot reveal a
    # split brain — "2 sent" reads identically whether it went to prod or to
    # staging — and the state an operator is in when that matters most is
    # mid-cutover, which is exactly when the sub-state is "buffering" rather
    # than the happy one.
    if not settings.ship:
        reason = "off — nothing is captured (aisquare init --explainability to turn it on)"
    elif not gateway_url:
        reason = f"on, but no gateway URL is configured ({GATEWAY_ENV_VAR})"
    else:
        destination = f"on → {gateway_url}"
        if not has_key:
            # Only offer the key file when it would actually be read. Telling
            # someone to write a file we will then ignore is worse than silence.
            where = f"set ${key_env}"
            if key_env == KEY_ENV_VAR:
                where += f" or write {key_path()}"
            reason = f"{destination} — but no workspace key: {where}"
        elif not sdk:
            reason = (
                f"{destination} — buffering, the explainability extra is missing: {install_hint()}"
            )
        elif counts.queued:
            reason = (
                f"{destination} — {counts.queued} buffered, "
                "run 'aisquare explainability ship' to drain"
            )
        else:
            reason = f"{destination} — nothing buffered"
    return ShippingState(
        configured=settings.ship,
        gateway_url=gateway_url,
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
    gateway_url, key_env, target_key = _active_deployment()
    if not gateway_url:
        return ShipReport(reason=f"no gateway URL configured ({GATEWAY_ENV_VAR})")
    # The active target's key, then the key file. Never another deployment's:
    # shipping prod sessions with a staging key is worse than not shipping.
    api_key = target_key
    if api_key is None:
        where = f"set ${key_env}"
        if key_env == KEY_ENV_VAR:
            where += f" or write {key_path()}"
        return ShipReport(reason=f"no workspace key — {where}")
    if not sdk_available():
        pending = len(outbox.pending())
        return ShipReport(
            deferred=pending,
            reason=f"explainability extra not installed, {pending} buffered — {install_hint()}",
        )

    outbox.reclaim_stale()
    batch = outbox.pending(limit)
    if not batch:
        return ShipReport(reason="nothing buffered")

    try:
        sdk = _init_sdk(gateway_url, api_key)
    except Exception as exc:  # the SDK failing to start is a deferral, not a loss
        return ShipReport(
            deferred=len(batch), reason=f"explainability SDK would not initialise: {exc}"
        )

    return _drain(sdk, settings, batch)


def _init_sdk(gateway_url: str, api_key: str) -> Any:
    """Import and initialise the SDK from an environment we control.

    ``init_from_env`` is the SDK's own contract, so we set the variables it
    reads rather than reaching past it into private setters — an SDK upgrade
    that changes its internals must not silently stop shipping.
    """
    os.environ[GATEWAY_ENV_VAR] = gateway_url
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
        claimed: list[tuple[Path, dict[str, object]]] = []
        for path in paths_for_run:
            handle = outbox.claim(path)
            if handle is None:  # another sweeper got there first
                continue
            record = outbox.load(handle)
            if record is None or record.get("v") not in _READABLE_RECORD_VERSIONS:
                outbox.mark_dead(handle, "unreadable or unsupported record")
                dead += 1
                continue
            claimed.append((handle, record))
        if not claimed:
            continue
        # Identity comes from the BOARD session, not from the run key: on a
        # launch that could not be joined those differ, and a role looked up by
        # pipeline id would miss and file a planner's Run under the generic
        # identity. The board id travels in the record for exactly this.
        agent_name = _agent_name_for(settings, _board_session_of(claimed))
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
    """Bucket spooled records by the Run they belong to, order preserved.

    ``run_key`` is the pipeline id the capturing process was already living
    under and is the Run's identity; the board ``session_id`` is the fallback
    for records captured outside a traced session, and for spool files written
    by an older CLI that predates the key.
    """
    grouped: dict[str, list[Path]] = {}
    for path in batch:
        record = outbox.load(path) or {}
        key = str(record.get("run_key") or record.get("session_id") or "") or UNATTRIBUTED_RUN
        grouped.setdefault(key, []).append(path)
    return grouped


#: Records with no session id (a plain `aisquare note` from a shell, say) still
#: belong somewhere. One bucket rather than one Run each: a hundred one-span
#: Runs is exactly the fragmentation the run doctrine forbids.
UNATTRIBUTED_RUN = "aisquare-cli-unattributed"

#: Spool schemas this sweeper can replay. A record from a NEWER CLI is
#: dead-lettered rather than guessed at — shipping a span whose fields you have
#: mis-read is worse than not shipping it, because it looks delivered. Older
#: schemas stay readable: an upgrade mid-shift must not orphan a full queue.
_READABLE_RECORD_VERSIONS = frozenset(range(1, insights.RECORD_VERSION + 1))


def _board_session_of(claimed: list[tuple[Path, dict[str, object]]]) -> str:
    """The board session id these records came from, or the unattributed bucket."""
    for _, record in claimed:
        session_id = str(record.get("session_id") or "")
        if session_id:
            return session_id
    return UNATTRIBUTED_RUN


def _agent_name_for(settings: ExplainabilitySettings, session_id: str) -> str:
    """The registered identity this Run is attributed to.

    Resolved from the session's board role, so a planner's Run is not a coder's
    — the gateway collapses runs and costs under a shared name. Looking the
    role up costs a store read, which is fine HERE: the sweeper is off the
    primary path, which is the entire reason it exists.
    """
    role = "cli"
    if session_id != UNATTRIBUTED_RUN:
        try:
            with store_session() as store:
                session = store.get_session(session_id)
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
    #: agree to it, not discover it in a dashboard afterwards. It names the
    #: redaction level rather than promising safety, because the level is the
    #: thing they can change and "we redact secrets" is a claim no pattern list
    #: can honestly make.
    CAPTURES = (
        "your prompts, board notes, task claims and session events "
        "(no file contents, no model traffic), credentials redacted at "
        "config.redaction.level"
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
            reason=f"explainability extra not installed ({install_hint()})",
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
