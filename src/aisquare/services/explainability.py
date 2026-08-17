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

THE CORRELATION SPINE. ``X-Pipeline-Id`` is only useful if the board row for
the same session can be found from it — otherwise the two datasets (board
tasks, claims and notes on one side, gateway Runs on the other) cannot be
joined at all. The board's key is the agent's own session id, which only the
agent knows, and it reports it to the ``SessionStart`` hook. So the join is
made in two halves that meet inside the agent:

1. The LAUNCHER mints the pipeline id, wires the headers, and leaves the id in
   the child's environment (``trace_marker``). Env is binary-agnostic, which
   is the whole point: since #57 a role can be bound to any executable, and a
   wrapper that has never heard of ``--session-id`` still passes its
   environment to what it runs.
2. The HOOK inside the agent reads that id back (``traced_by``), pairs it with
   the session id Claude Code just handed it — the very id the board row uses
   — and appends the mapping (``record_join``).

``plan_session_identity`` is a strict *extra* on top: when the resolved binary
is literally ``claude`` and the caller named no session of their own, the
launcher passes ``--session-id <pipeline id>`` so the two ids are not merely
joinable but identical. It gives up easily and says why, because half 2
already guarantees the join.

The join log is JSON Lines at ``~/.aisquare/explainability/joins.jsonl``
(``AISQUARE_HOME`` relocates it), one object per observed session start::

    {"started_at": "2026-08-17T02:31:07+00:00",  # UTC, ISO 8601
     "session_id": "5f1c…",   # the agent's session id == the board row id
     "pipeline_id": "5f1c…",  # X-Pipeline-Id, the gateway Run key
     "agent_name": "aisquare-coder",   # X-Agent-Name, the studio identity
     "role": "coder",
     "cwd": "/home/me/repo"}

Append-only, and written on a path that never reads it back, so it cannot slow
a session start down or grow into shared state. Rows are observations rather
than records: one session seen twice (a ``/clear``, a resume) appends twice,
and a reader dedupes on ``(session_id, pipeline_id)``.

Everything here fails open by design. Tracing is an observer — the moment it
would break a launch (proxy down, wrong proxy mode, an ANTHROPIC_* var the
user already owns, a config typo, an unwritable join log) we launch the
session untraced and say why, we never block. The only hard opt-in is
``explainability.enabled`` in ``~/.aisquare/config.toml``, which defaults to
off.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from aisquare.core import paths
from aisquare.core.config import ExplainabilitySettings

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

#: The ONE program we know accepts ``--session-id <uuid>`` (verified on Claude
#: Code 2.1.233). Matched on the basename, so an absolute path counts, and
#: nothing else does — not ``claude2``, not a wrapper. Since #57 a role can be
#: bound to any executable, and handing an unknown flag to one would kill the
#: launch. Every other binary joins through the hook seam instead, which needs
#: no flag, so the narrow match costs nothing.
_SESSION_ID_AGENT = "claude"

#: Flags whose presence means the caller (or the human) already owns the
#: session id, so we must read it rather than choose one.
_SESSION_ID_FLAG = "--session-id"
_RESUME_FLAGS = ("--resume", "-r")
_CONTINUE_FLAGS = ("--continue", "-c")

#: Escape hatch for the launch that pinning breaks. Set to 0/false/no/off to
#: keep the launcher out of the agent's argv; sessions then join through the
#: hook seam alone, which is the same join by a different route.
_PIN_ENV_VAR = "AISQUARE_PIN_SESSION_ID"
_OFF_VALUES = {"0", "false", "no", "off"}

#: Carried INTO every traced agent process, and the reason this design needs
#: no flag: the hook that runs inside the agent is the only place that knows
#: the board session id, and these tell it which Run that session belongs to.
#: Env is binary-agnostic — a wrapper that ignores them loses nothing, and a
#: wrapper that passes its environment on (all of them do) joins for free.
PIPELINE_ID_ENV_VAR = "AISQUARE_PIPELINE_ID"
AGENT_NAME_ENV_VAR = "AISQUARE_AGENT_NAME"


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


def accepts_session_id(binary: str) -> bool:
    """Whether ``binary`` is the one program we may hand ``--session-id`` to."""
    if os.environ.get(_PIN_ENV_VAR, "").strip().lower() in _OFF_VALUES:
        return False
    return os.path.basename(binary) == _SESSION_ID_AGENT


def plan_session_identity(binary: str, args: Sequence[str]) -> SessionIdentity:
    """Decide the id this launch is traced under, before anything is launched.

    This is the *optional strict* half of the spine: when we can make the
    agent's own session id equal the pipeline id, the two datasets need no
    lookup at all. It is deliberately the half that gives up easily, because
    the hook seam already guarantees a join for every launch — so anything
    doubtful here costs a nicety, never the correlation.

    Four cases, in order. The caller already named a session id
    (``--session-id``) or a session to resume: read it, inject nothing. The
    caller asked for a session that does not exist yet (``--continue``, a bare
    ``--resume``): we cannot name it in advance, and *guessing* would be worse
    than not trying — two agents on one id merge into a single board row. The
    binary is not ``claude``: same answer, because an unknown flag kills the
    launch. Otherwise mint a fresh id and hand it over.
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


def trace_marker(wiring: SessionWiring) -> dict[str, str]:
    """The env a traced agent carries so its OWN hook can record the join.

    Empty for an untraced launch — there is no Run to point at, and an agent
    carrying a stale marker would make its hook write a join that is not true.
    """
    if not wiring.traced or not wiring.pipeline_id:
        return {}
    marker = {PIPELINE_ID_ENV_VAR: wiring.pipeline_id}
    if wiring.agent_name:
        marker[AGENT_NAME_ENV_VAR] = wiring.agent_name
    return marker


def traced_by(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """``(pipeline_id, agent_name)`` this process was launched to trace.

    ``None`` when the process was not launched by a traced launcher — which is
    every ordinary session, so callers on the hook path can leave immediately.
    """
    source = os.environ if env is None else env
    pipeline_id = (source.get(PIPELINE_ID_ENV_VAR) or "").strip()
    if not pipeline_id:
        return None
    return pipeline_id, (source.get(AGENT_NAME_ENV_VAR) or "").strip()


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
    environment. No flag, no binary-specific knowledge, so a role bound to a
    wrapper joins exactly like the default agent does.

    Returns ``None`` on success and a reason otherwise — never raises. Rows are
    observations, not state: a session seen twice (a ``/clear``, a resume)
    appends twice, and readers dedupe on ``(session_id, pipeline_id)``. That
    keeps the hook path a single append with nothing to read first.
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

    Deliberately shallow — scheme and host, nothing about reachability, which
    is the probe's job. It exists to stop a value the AGENT cannot parse from
    reaching its environment, because that failure mode is not a lost trace:
    the session dies at the first request with a non-zero exit.
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

    # ANTHROPIC_BASE_URL is the ONE value here the agent parses before it can
    # report anything, so a malformed one does not cost a trace — it kills the
    # session with "API Error: Invalid URL" and a non-zero exit, which is
    # tracing costing a launch. Checked ahead of the probe because the probe's
    # failure message would blame the network for a typo in config.
    if not _usable_base_url(settings.proxy_url):
        return SessionWiring(
            traced=False,
            reason=f"explainability.proxy_url {settings.proxy_url!r} is not an "
            "http(s) URL — launching untraced",
        )

    if base_env:
        taken = [name for name in RESERVED_ENV_VARS if base_env.get(name)]
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
