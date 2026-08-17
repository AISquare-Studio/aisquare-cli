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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
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

#: Agents whose CLI is known to accept ``--session-id <uuid>``: the claude
#: binary and the parallel installs people name after it (``claude2``,
#: ``claude-next``). Matched on the BASENAME, so an absolute path works too.
#: Anything else launches with the agent's own id and traces unjoined — a
#: flag an agent does not understand is a dead launch, and no trace is worth
#: that. Rename a wrapper to something not starting with "claude" to opt out.
_SESSION_ID_AGENT = re.compile(r"^claude[0-9A-Za-z._-]*$")

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

#: Variable ``aisquare explainability env`` exports alongside the header pair,
#: so a shell command can pass the same id to the agent it is about to run.
SESSION_ID_ENV_VAR = "AISQUARE_SESSION_ID"


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
    """Whether ``binary`` is one we may hand ``--session-id`` to."""
    if os.environ.get(_PIN_ENV_VAR, "").strip().lower() in _OFF_VALUES:
        return False
    return bool(_SESSION_ID_AGENT.match(os.path.basename(binary)))


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
            None, note=f"{os.path.basename(binary)!r} is not known to accept {_SESSION_ID_FLAG}"
        )
    session_id = str(uuid.uuid4())
    return SessionIdentity(session_id, inject_args=(_SESSION_ID_FLAG, session_id))


def record_join(
    *,
    session_id: str | None,
    agent_name: str,
    pipeline_id: str,
    role: str,
    cwd: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Append this launch to the join log; return why it could not be written.

    Returns ``None`` on success and a reason otherwise — never raises. The log
    is a convenience for joining board rows to Runs without dashboard access,
    which makes it strictly less important than the launch it describes.
    """
    stamped = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    record = {
        "started_at": stamped,
        "session_id": session_id,
        "agent_name": agent_name,
        "pipeline_id": pipeline_id,
        "joined": session_id is not None and session_id == pipeline_id,
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
