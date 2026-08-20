"""The wire contract between aisquare's hooks and the CI experiment endpoint.

One endpoint, one payload shape, four triggers. The CLI never learns which
architecture serves it — a new server-side capability must require zero CLI
changes, so nothing here branches on what came back beyond the four actions.

Everything in this module is total. :func:`parse_response` has no failure mode
that reaches its caller: a 500, a truncated body, a field that changed type, or
a contract revision this build has never heard of all resolve to :data:`ALLOW`,
carrying a :class:`DegradationReason` that says which one happened.

That reason is not diagnostics — it is load-bearing experimental data.
``action == "allow"`` is both what a healthy server returns when it has nothing
useful to add *and* what a dead one looks like from the outside. Recorded
without the reason beside it, an endpoint failing every single call is
indistinguishable from a clean baseline, and the experiment measures nothing
while appearing perfectly healthy. Callers persist :attr:`Outcome.reason`; they
do not merely log it.

Versioning is checked before interpretation. A response whose ``contract`` is
not :data:`CONTRACT_VERSION` degrades rather than being read under this build's
assumptions, because guessing across a schema change produces results that are
wrong instead of absent — and wrong results are the ones that get published.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

CONTRACT_VERSION = 1
"""The contract revision this build speaks; bumped only by a breaking change."""

CONTRACT_HEADER = "X-CI-Contract"
"""Request header carrying :data:`CONTRACT_VERSION`."""

ADVISORY_BUDGET_MS = 400
"""The deadline declared to the server as ``budget_ms``.

Advisory. The server owns its own timing; this says what the client would like
so a slow path can be shed server-side, where there is enough information to
shed the right thing.
"""

CLIENT_BACKSTOP_SECONDS = 10.0
"""The only deadline the client actually enforces.

This deviates from the original spec deliberately. §02 made ``budget_ms`` a
hard client-side deadline — exceed it, treat as allow — but at 400ms a server
needing 300ms over a 100ms network fails open on most calls, and per the module
docstring that failure is invisible in the recorded action. A tight client
clamp converts a latency problem into silently absent data.

So the client keeps only a backstop against a hung endpoint holding a
developer's prompt hostage. Ten seconds sits under Claude Code's 30s
``UserPromptSubmit`` cancellation, so the hook always returns a decision of its
own instead of having its output discarded — which would degrade with no
reason recorded at all.
"""


class Trigger(StrEnum):
    """Why the CLI is calling; the only field that varies the request shape."""

    session_start = "session_start"
    prompt_submit = "prompt_submit"
    tool_intercept = "tool_intercept"
    agent_request = "agent_request"


class Action(StrEnum):
    """What the server asks the CLI to do."""

    inject = "inject"
    """Add ``context`` to what the agent sees. The Phase 1 workhorse."""

    substitute = "substitute"
    """Return ``tool_result`` instead of running the tool.

    No consumer in Phase 1, and its delivery mechanism is unsettled: Claude
    Code's ``PreToolUse`` hook cannot fabricate a tool result. It exposes
    ``permissionDecision`` (allow/deny/ask), ``permissionDecisionReason`` and
    ``updatedInput`` — and a call with ``updatedInput`` still executes. The
    nearest approximation is a deny carrying the payload in its reason, which
    reaches the agent framed as a refusal rather than as a result. Kept in the
    contract so the server can express the intent; read the bilateral decisions
    in ``docs/ci-contract.md`` before wiring it to anything.
    """

    allow = "allow"
    """Proceed untouched. Also the value every degraded call resolves to."""

    noop = "noop"
    """The server ran and had nothing to add. Distinct from ``allow`` only by
    intent, and distinguishable from a degraded call only via
    :class:`DegradationReason`."""


class DegradationReason(StrEnum):
    """Why a call carries no server decision.

    Persisted beside the action on every recorded call. :attr:`none` means the
    server genuinely decided; everything else means it did not, and any
    aggregate that mixes the two is measuring its own plumbing.
    """

    none = "none"
    """The server answered and this build understood it."""

    not_configured = "not_configured"
    """No endpoint configured — the default state. No request was made."""

    disabled = "disabled"
    """Configured but switched off. No request was made."""

    transport_error = "transport_error"
    """Connection refused, DNS failure, TLS failure, socket reset."""

    backstop_exceeded = "backstop_exceeded"
    """:data:`CLIENT_BACKSTOP_SECONDS` elapsed. A server-side bug, not a result."""

    http_error = "http_error"
    """A response arrived with a status other than 200."""

    malformed_body = "malformed_body"
    """The body was not JSON, or was JSON but not an object."""

    contract_mismatch = "contract_mismatch"
    """``contract`` names a revision this build does not speak."""

    unknown_action = "unknown_action"
    """``action`` is absent or names something outside :class:`Action`."""

    schema_mismatch = "schema_mismatch"
    """Right contract and known action, but a field failed validation."""


class ToolRef(BaseModel):
    """The tool call a ``tool_intercept`` request is about."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class HookRequest(BaseModel):
    """One call to ``POST {AISQUARE_CI_URL}/v1/hook``."""

    trigger: Trigger
    session_id: str
    trace_id: str
    project_id: str
    budget_ms: int = ADVISORY_BUDGET_MS

    run_id: str | None = None
    """Server-minted run key; ``None`` outside an experiment.

    Never generated here. The format in §02 (``r-20260819-0134``) is the
    server's, and a CLI that mints its own would silently fork the run space.
    """

    arm: str | None = None
    """Opaque to the CLI, by design. Branching on it here would put experiment
    logic in the client, where changing it costs a release."""

    snapshot_ref: str | None = None
    prompt: str | None = None
    """``prompt_submit`` only."""

    tool: ToolRef | None = None
    """``tool_intercept`` only."""

    def to_wire(self) -> dict[str, Any]:
        """The JSON-ready body, nulls included as §02 shows them."""
        return self.model_dump(mode="json")


class Provenance(BaseModel):
    """Where one piece of returned context came from."""

    node_id: str
    source: str


class CacheHint(BaseModel):
    """How long the caller may reuse this response, and under what key."""

    ttl_s: int
    key: str


class HookResponse(BaseModel):
    """A server decision, already known to be contract-compatible."""

    contract: int
    action: Action
    context: str | None = None
    """``inject`` only: the markdown block to put in front of the agent."""

    tool_result: str | None = None
    """``substitute`` only."""

    provenance: list[Provenance] = Field(default_factory=list)
    flags_applied: list[str] = Field(default_factory=list)
    server_ms: int | None = None
    """The server's own timing, so network cost can be reported separately
    rather than silently folded into the comparison."""

    cache_hint: CacheHint | None = None


ALLOW = HookResponse(contract=CONTRACT_VERSION, action=Action.allow)
"""The response every degraded call resolves to. Never mutate it."""


@dataclass(frozen=True)
class Outcome:
    """A response together with why it is the response.

    Returned by :func:`parse_response` and by every client call, so a caller
    cannot record the action without having the reason in hand.
    """

    response: HookResponse
    reason: DegradationReason
    detail: str = ""
    """Free text for logs. Never parsed, never aggregated on."""

    @property
    def degraded(self) -> bool:
        """Whether the server failed to decide this call."""
        return self.reason is not DegradationReason.none


def degraded(reason: DegradationReason, detail: str = "") -> Outcome:
    """An allow outcome carrying why the server did not produce one."""
    return Outcome(response=ALLOW, reason=reason, detail=detail)


def parse_response(*, status: int, body: str) -> Outcome:
    """Turn a raw HTTP response into an :class:`Outcome`. Never raises.

    Reasons are checked in the order that keeps each one reachable and exact:
    contract before action, action before full validation. Validating first
    would report every skewed response as ``schema_mismatch`` and lose the
    distinction between "we do not speak this revision" — recoverable by
    upgrading — and "this build and this server disagree about a field", which
    is a bug in one of them.
    """
    if status != 200:
        return degraded(DegradationReason.http_error, f"status {status}")
    try:
        raw = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return degraded(DegradationReason.malformed_body, "body is not JSON")
    if not isinstance(raw, dict):
        return degraded(DegradationReason.malformed_body, f"body is {type(raw).__name__}")
    if raw.get("contract") != CONTRACT_VERSION:
        return degraded(
            DegradationReason.contract_mismatch,
            f"server speaks {raw.get('contract')!r}, this build speaks {CONTRACT_VERSION}",
        )
    action = raw.get("action")
    if not isinstance(action, str) or action not in set(Action):
        return degraded(DegradationReason.unknown_action, f"action {action!r}")
    try:
        response = HookResponse.model_validate(raw)
    except ValidationError as exc:
        return degraded(DegradationReason.schema_mismatch, _first_error(exc))
    return Outcome(response=response, reason=DegradationReason.none)


def _first_error(exc: ValidationError) -> str:
    """The first validation failure as ``field: message``, for the log line."""
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return "validation failed"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
    return f"{location}: {first.get('msg', 'invalid')}"
