"""Hook contract v2: the wire between aisquare's hooks and the CI server.

The authority for every field here is the server's schema, vendored byte for
byte under ``tests/fixtures/ci_contract/v2/schemas`` together with its
fixtures. These models mirror those schemas exactly — every pattern, every
cross-field rule, ``additionalProperties: false`` at every level — and the
suite proves the mirror against the schemas themselves with ``jsonschema``,
not against a second reading of them. A scorer with its own predicate can only
ever agree with itself.

Three things this module is structurally unable to do, on purpose:

**It cannot learn which arm it is in.** The delivery descriptor is the only run
document the client fetches, and it carries no architecture, source, reader or
arm field. Nothing here has a slot for one, so nothing downstream can branch on
one.

**It cannot grant scope.** The request carries no workspace, studio, project or
principal id — ``project_ref`` selects context and grants nothing. The server
resolves authority from the bearer token.

**It cannot block, substitute or fabricate a tool result.** ``inject`` and
``noop`` are the only actions. ``allow``, ``block``, ``substitute`` and
``tool_result`` are named by canon as *not* contract-v2 actions and are
rejected at the enum.

Everything is total. :func:`parse_response` has no failure mode that reaches
its caller: a 500, a truncated body, 20 000 levels of nesting, a field that
changed type, or a contract revision this build has never heard of all resolve
to an :class:`Outcome` with no response and a :class:`ClientReason` saying
which one happened. That reason is load-bearing experimental data, not
diagnostics — a dead endpoint and a server with nothing to add both inject
nothing, and recorded without the reason beside it, an endpoint failing every
call is indistinguishable from a clean baseline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aisquare.models import (
    BriefingStatus,
    CacheStatus,
    ClientReason,
    HookAction,
    HookTrigger,
)

CONTRACT_VERSION = 2
"""The hook contract this build speaks. A body whose ``contract`` is anything
else — including ``true``, which ``== 1`` in Python — is a mismatch."""

RECALL_TOOL = "collective_intelligence_recall"
"""The one read-only MCP tool, named by the descriptor and by canon."""

RECALL_ROUTE = "/v1/mcp/"
"""Server-relative prefix of the pull route. The descriptor's ``mcp_pull.tool``
completes it: ``POST {base}/v1/mcp/collective_intelligence_recall`` takes
``mcp-tool-input.v1`` and answers with a bare ``mcp-tool-output.v1`` briefing —
no hook envelope, the ``status`` is inside (``app/api/delivery.py``)."""

MAX_PROMPT_CHARS = 100_000
"""``hook-request.prompt`` ``maxLength``. Longer prompts are clipped before they leave."""

MAX_REASON_CHARS = 2_000
"""``mcp-tool-input.reason`` ``maxLength``. Scrubbed text is clipped to it before it leaves."""

MAX_DETAIL_CHARS = 200
"""Cap on any server-controlled text interpolated into an outcome's ``detail``,
so a 200 KB ``contract`` value costs 200 characters, not 200 KB per call."""

# The three prefixed id shapes and the two hash shapes the schemas pin. A
# ``ws_`` or ``std_`` value cannot ride in through an id field, because the
# prefix is part of the pattern.
_ID_TAIL = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
RUN_ID = re.compile(r"^run_" + _ID_TAIL)
SESSION_ID = re.compile(r"^ses_" + _ID_TAIL)
TRACE_ID = re.compile(r"^trc_" + _ID_TAIL)
QUERY_ID = re.compile(r"^qry_" + _ID_TAIL)
BRIEFING_ID = re.compile(r"^brf_" + _ID_TAIL)
CHECKPOINT_ID = re.compile(r"^ckp_" + _ID_TAIL)
ITEM_ID = re.compile(r"^ki_" + _ID_TAIL)
CONFIG_ID = re.compile(r"^cfg_public_" + _ID_TAIL)
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_UUID5 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
RFC3339_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")
_ENDPOINT = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)
"""Every wire model: unknown keys fail (``additionalProperties: false``), values
are immutable once parsed (a shared outcome can never be mutated by one caller
and read by the next), and no type coercion — the schema says ``integer`` and
``"63"`` is not one."""


def _match(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not pattern.match(value):
        raise ValueError(f"{what} does not match {pattern.pattern}")
    return value


def wire_session_id(session_id: str) -> str:
    """The ``ses_…`` form of an agent session id, always schema-valid.

    Seam decision J2: ``ses_`` + the Claude Code session id (a UUID, which fits
    the pattern's 64-character tail). Kept in one function so the rule is one
    edit if the ids are settled differently. Any character the pattern cannot
    carry becomes ``-`` and the tail is clipped, so a value the agent hands us
    can never produce a request the server rejects on shape alone.
    """
    raw = session_id.removeprefix("ses_")
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", raw)[:64]
    if not safe or not re.match(r"[A-Za-z0-9]", safe[0]):
        safe = "x" + safe[:63]
    return "ses_" + safe


def observed_now() -> str:
    """The client clock as the schema wants it: UTC, RFC 3339, millisecond, ``Z``."""
    now = datetime.now(tz=UTC)
    return f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}Z"


# --- request ------------------------------------------------------------------


class HookRequest(BaseModel):
    """``hook-request.experimental-v2`` — one push-delivery call to the server.

    All ten fields are required and sent, nulls included; the schema has no
    optional key. Construction validates every pattern and the two conditional
    rules, so a request that could not pass the server's validator is never
    built, let alone sent.
    """

    model_config = _STRICT

    contract: Literal[2] = 2
    trigger: HookTrigger
    run_id: str
    session_id: str
    trace_id: str
    project_ref: str | None = Field(default=None, min_length=1, max_length=500)
    """``<owner/repo>@<branch>`` — a selector for context. Grants nothing."""
    snapshot_ref: str | None = None
    """A git object id (40 hex), a content hash (64 hex) or a ``ckp_`` checkpoint.
    Never a ref *name*: ``refs/aisquare/wip/…`` is where the CLI keeps the
    object alive locally, the id is what travels."""
    prompt: str | None = Field(default=None, min_length=1, max_length=MAX_PROMPT_CHARS)
    client_safety_ms: int = Field(ge=1)
    client_observed_at: str

    @model_validator(mode="after")
    def _shape(self) -> HookRequest:
        _match(RUN_ID, self.run_id, "run_id")
        _match(SESSION_ID, self.session_id, "session_id")
        _match(TRACE_ID, self.trace_id, "trace_id")
        _match(RFC3339_Z, self.client_observed_at, "client_observed_at")
        if self.snapshot_ref is not None and not (
            _GIT_OBJECT.match(self.snapshot_ref)
            or _SHA256_HEX.match(self.snapshot_ref)
            or CHECKPOINT_ID.match(self.snapshot_ref)
        ):
            raise ValueError("snapshot_ref must be a git object id, a sha256 hex or a ckp_ id")
        if self.trigger == "session_start":
            if self.prompt is not None:
                raise ValueError("prompt must be null on session_start")
        elif self.prompt is None:
            raise ValueError(f"prompt is required on {self.trigger}")
        return self

    def to_wire(self) -> dict[str, Any]:
        """The JSON-ready body, nulls included as the schema requires them."""
        return self.model_dump(mode="json")


# --- response -----------------------------------------------------------------


class Freshness(BaseModel):
    model_config = _STRICT

    status: Literal["current", "stale", "unknown"]
    basis: str = Field(min_length=1, max_length=200)
    action: Literal["include", "rank_penalty"]


class BriefingItem(BaseModel):
    """One knowledge item in a briefing. Closed: a ``source_kind`` here would
    name the arm, which is exactly what the client must not be able to read."""

    model_config = _STRICT

    item_id: str
    item_version: int = Field(ge=1)
    text: str = Field(min_length=1)
    structured_facts: dict[str, Any]
    evidence_ids: list[str] = Field(min_length=1)
    freshness: Freshness

    @model_validator(mode="after")
    def _shape(self) -> BriefingItem:
        _match(ITEM_ID, self.item_id, "item_id")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")
        for evidence in self.evidence_ids:
            _match(_UUID5, evidence, "evidence_id")
        return self


class CacheReport(BaseModel):
    model_config = _STRICT

    status: CacheStatus
    key_hash: str
    age_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _shape(self) -> CacheReport:
        _match(SHA256, self.key_hash, "cache.key_hash")
        return self


class TimingReport(BaseModel):
    model_config = _STRICT

    scope: int = Field(ge=0)
    candidate: int = Field(ge=0)
    rank: int = Field(ge=0)
    compose: int = Field(ge=0)
    total: int = Field(ge=0)


class Briefing(BaseModel):
    """``mcp-tool-output.v1`` — the reader's complete answer, including its own
    failure. The same object rides inside a hook response as ``briefing`` and
    is the MCP recall tool's result, which is what lets push and pull be
    compared at all.
    """

    model_config = _STRICT

    briefing_id: str | None
    run_id: str
    query_id: str
    config_fingerprint: str
    input_checkpoint: str
    resolved_scope_version: int = Field(ge=0)
    items: list[BriefingItem]
    rendered_context: str
    """Server-rendered, byte-identical across arms by construction. The CLI
    frames it; it never rewrites it."""
    token_count: int = Field(ge=0)
    cache: CacheReport
    timing_ms: TimingReport
    status: BriefingStatus

    @model_validator(mode="after")
    def _shape(self) -> Briefing:
        _match(RUN_ID, self.run_id, "run_id")
        _match(QUERY_ID, self.query_id, "query_id")
        _match(SHA256, self.config_fingerprint, "config_fingerprint")
        _match(CHECKPOINT_ID, self.input_checkpoint, "input_checkpoint")
        if self.briefing_id is not None:
            _match(BRIEFING_ID, self.briefing_id, "briefing_id")
        if self.status == "served":
            if not self.items:
                raise ValueError("a served briefing must carry at least one item")
            if self.briefing_id is None:
                raise ValueError("a served briefing must carry a briefing_id")
        elif self.status == "empty":
            if self.items:
                raise ValueError("an empty briefing must carry no items")
        elif self.status == "unavailable":
            if self.items or self.rendered_context or self.token_count:
                raise ValueError("an unavailable briefing must carry nothing")
        return self


class ErrorRecord(BaseModel):
    """``error.v1`` — one server-reported failure.

    ``code`` and ``http_status`` are deliberately looser than the schema's
    closed catalog: that catalog is the server's, and a code this build has
    never seen is data to record verbatim, not a reason to throw away a
    response that is otherwise valid (seam doc J15). Everything else mirrors
    the schema, and the shape stays closed.
    """

    model_config = _STRICT

    schema_version: Literal["error/v1"]
    code: str = Field(min_length=1, max_length=MAX_DETAIL_CHARS)
    http_status: int
    message: str = Field(min_length=1, max_length=2000)
    subject_ref: str | None = Field(default=None, min_length=1, max_length=500)
    retryable: bool
    detail: dict[str, Any]
    occurred_at: str

    @model_validator(mode="after")
    def _shape(self) -> ErrorRecord:
        _match(RFC3339_Z, self.occurred_at, "occurred_at")
        return self


class Deadline(BaseModel):
    model_config = _STRICT

    server_ms: int = Field(ge=1)
    client_safety_ms: int = Field(ge=1)
    breached: bool


class HookResponse(BaseModel):
    """``hook-response.experimental-v2`` — a server decision, contract-checked.

    The seven cross-field rules are the part a permissive parser would lose:
    ``inject`` with no briefing is not "inject nothing", it is a broken server,
    and it must land as ``schema_mismatch`` rather than as a healthy row that
    injected nothing.
    """

    model_config = _STRICT

    contract: Literal[2]
    status: BriefingStatus
    action: HookAction
    briefing: Briefing | None
    config_fingerprint: str | None
    server_ms: int = Field(ge=0)
    deadline: Deadline
    errors: list[ErrorRecord]

    @model_validator(mode="after")
    def _shape(self) -> HookResponse:
        if self.config_fingerprint is not None:
            _match(SHA256, self.config_fingerprint, "config_fingerprint")
        if self.status in ("empty", "unavailable") and self.action != "noop":
            raise ValueError(f"status {self.status} requires action noop")
        if self.status == "served" and self.action != "inject":
            raise ValueError("status served requires action inject")
        if self.action == "inject" and (self.briefing is None or self.config_fingerprint is None):
            raise ValueError("action inject requires a briefing and a config_fingerprint")
        if self.action == "noop" and self.briefing is not None:
            raise ValueError("action noop requires briefing null")
        if self.status in ("degraded", "unavailable") and not self.errors:
            raise ValueError(f"status {self.status} requires at least one error")
        if self.status in ("served", "empty") and self.errors:
            raise ValueError(f"status {self.status} requires errors to be empty")
        if self.deadline.breached and self.status != "unavailable":
            raise ValueError("a breached deadline requires status unavailable")
        return self


# --- descriptor ---------------------------------------------------------------


class DirectApiDelivery(BaseModel):
    model_config = _STRICT

    kind: Literal["direct_api"]


class HookPushDelivery(BaseModel):
    model_config = _STRICT

    kind: Literal["hook_push"]
    triggers: list[HookTrigger] = Field(min_length=1)
    endpoint: str

    @model_validator(mode="after")
    def _shape(self) -> HookPushDelivery:
        if len(set(self.triggers)) != len(self.triggers):
            raise ValueError("triggers must be unique")
        _match(_ENDPOINT, self.endpoint, "endpoint")
        return self


class McpPullDelivery(BaseModel):
    model_config = _STRICT

    kind: Literal["mcp_pull"]
    tool: Literal["collective_intelligence_recall"]


DeliveryMode = DirectApiDelivery | HookPushDelivery | McpPullDelivery


class DeliveryDescriptor(BaseModel):
    """``client-delivery-descriptor.v1`` — the only run document a client fetches.

    Blinding by construction: the descriptor says *how* to deliver and never
    *what* is serving. A client that can read which arm it is in is a client
    whose behaviour can vary with the arm, and this model has no field for it
    (the vendored invalid fixture is exactly an ``arm_kind`` being refused).
    """

    model_config = _STRICT

    contract_version: Literal[2]
    run_id: str
    opaque_config_id: str
    delivery: list[DeliveryMode] = Field(min_length=1)
    client_safety_ms: int = Field(ge=1)
    """The client's hang ceiling for this run, enforced as wall-clock. It
    replaces any constant the client might have had."""
    retry_policy: Literal["none"]
    expires_at: str

    @model_validator(mode="after")
    def _shape(self) -> DeliveryDescriptor:
        _match(RUN_ID, self.run_id, "run_id")
        _match(CONFIG_ID, self.opaque_config_id, "opaque_config_id")
        _match(RFC3339_Z, self.expires_at, "expires_at")
        kinds = [mode.kind for mode in self.delivery]
        if len(set(kinds)) != len(kinds):
            raise ValueError("at most one delivery member per kind")
        if "direct_api" in kinds and len(kinds) != 1:
            raise ValueError("direct_api must be the only delivery member when present")
        return self

    @property
    def hook_push(self) -> HookPushDelivery | None:
        return next((m for m in self.delivery if isinstance(m, HookPushDelivery)), None)

    @property
    def mcp_pull(self) -> McpPullDelivery | None:
        return next((m for m in self.delivery if isinstance(m, McpPullDelivery)), None)

    def pushes(self, trigger: HookTrigger) -> bool:
        """Whether the descriptor asks the CLI to call the hook on ``trigger``."""
        push = self.hook_push
        return push is not None and trigger in push.triggers

    def expires(self) -> datetime:
        """``expires_at`` as an aware datetime."""
        return datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(tz=UTC)) >= self.expires()


# --- MCP tool input -----------------------------------------------------------


class RecallInput(BaseModel):
    """``mcp-tool-input.v1`` — the recall tool's arguments, and nothing else.

    Closed so a ``workspace_id`` offered as authority is a validation failure
    naming the key, not a field the server accepts and ignores while the
    caller's code reads as if it scoped the request.
    """

    model_config = _STRICT

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    session_id: str
    run_id: str | None = None
    token_budget: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)

    @model_validator(mode="after")
    def _shape(self) -> RecallInput:
        _match(SESSION_ID, self.session_id, "session_id")
        if self.run_id is not None:
            _match(RUN_ID, self.run_id, "run_id")
        return self

    def to_wire(self) -> dict[str, Any]:
        """The JSON-ready body. The three optional keys are *absent* when unset,
        not null: the schema types them as string/integer without ``null``."""
        return self.model_dump(mode="json", exclude_none=True)


# --- outcome ------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """A parsed response together with why it is — or is not — usable.

    ``response`` is ``None`` exactly when ``reason`` is not :attr:`ClientReason.none`,
    so a caller cannot record an action without having the reason in hand, and
    cannot read a decision out of a call that did not produce one.
    """

    response: HookResponse | None
    reason: ClientReason
    detail: str = ""
    """Free text for logs, clipped. Never parsed, never aggregated on."""
    error_codes: tuple[str, ...] = ()
    """``error.v1`` codes the server put in the body of a non-200, verbatim.
    Empty unless the outcome is ``http_error`` with a body this build could
    read; a parsed response carries its codes in ``response.errors``."""

    @property
    def degraded(self) -> bool:
        return self.reason is not ClientReason.none

    @property
    def action(self) -> HookAction:
        """What the CLI should do. ``noop`` whenever the call degraded."""
        return "noop" if self.response is None else self.response.action

    @property
    def briefing(self) -> Briefing | None:
        return None if self.response is None else self.response.briefing


def degraded(
    reason: ClientReason, detail: str = "", *, error_codes: tuple[str, ...] = ()
) -> Outcome:
    """An outcome with no server decision, carrying why."""
    if reason is ClientReason.none:
        raise ValueError("a degraded outcome needs a reason other than none")
    return Outcome(response=None, reason=reason, detail=clip(detail), error_codes=error_codes)


def clip(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    """``text`` bounded to ``limit`` characters, marked when cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_contract_current(value: object) -> bool:
    """Exactly the integer :data:`CONTRACT_VERSION` — not ``True``, not ``2.0``."""
    return type(value) is int and value == CONTRACT_VERSION


def parse_response(*, status: int, body: str) -> Outcome:
    """Turn a raw HTTP response into an :class:`Outcome`. Never raises.

    The ladder runs in the order that keeps each reason reachable and exact:
    status, then JSON, then the contract version, then the full shape.
    Checking the version before validating the shape is what separates "this
    build does not speak that revision" — fixed by upgrading — from "these two
    builds disagree about a field", which is a bug in one of them.
    """
    if status != 200:
        detail, codes = http_failure(status, body)
        return degraded(ClientReason.http_error, detail, error_codes=codes)
    try:
        raw = json.loads(body)
    except Exception:  # RecursionError on deep nesting is not a ValueError
        return degraded(ClientReason.malformed_body, "body is not JSON")
    if not isinstance(raw, dict):
        return degraded(ClientReason.malformed_body, f"body is {type(raw).__name__}")
    if not is_contract_current(raw.get("contract")):
        return degraded(
            ClientReason.contract_mismatch,
            f"server speaks {clip(repr(raw.get('contract')), 80)}, this build speaks "
            f"{CONTRACT_VERSION}",
        )
    try:
        response = HookResponse.model_validate(raw)
    except ValidationError as exc:
        return degraded(ClientReason.schema_mismatch, first_error(exc))
    return Outcome(response=response, reason=ClientReason.none)


@dataclass(frozen=True)
class BriefingOutcome:
    """The pull route's answer, or why there is none — :class:`Outcome`'s sibling.

    The MCP route returns the briefing bare, so there is no envelope to hold a
    verdict beside it: ``briefing.status`` is the server's word, and
    ``briefing`` is ``None`` exactly when ``reason`` is not
    :attr:`ClientReason.none`.
    """

    briefing: Briefing | None
    reason: ClientReason
    detail: str = ""
    error_codes: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return self.reason is not ClientReason.none


def parse_briefing(*, status: int, body: str) -> BriefingOutcome:
    """Turn the pull route's raw response into a :class:`BriefingOutcome`. Never raises.

    The same ladder as :func:`parse_response` minus one rung: ``mcp-tool-output.v1``
    carries no ``contract`` field, so a server that moved on shows up as
    ``schema_mismatch`` naming the field rather than as ``contract_mismatch``.
    """
    if status != 200:
        detail, codes = http_failure(status, body)
        return BriefingOutcome(None, ClientReason.http_error, detail, codes)
    try:
        raw = json.loads(body)
    except Exception:
        return BriefingOutcome(None, ClientReason.malformed_body, "body is not JSON")
    if not isinstance(raw, dict):
        return BriefingOutcome(None, ClientReason.malformed_body, f"body is {type(raw).__name__}")
    try:
        briefing = Briefing.model_validate(raw)
    except ValidationError as exc:
        return BriefingOutcome(None, ClientReason.schema_mismatch, first_error(exc))
    return BriefingOutcome(briefing, ClientReason.none)


def first_error(exc: ValidationError) -> str:
    """The first validation failure as ``field: message``, clipped, for the log line."""
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return "validation failed"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
    return clip(f"{location}: {first.get('msg', 'invalid')}")


def parse_error(body: str) -> ErrorRecord | None:
    """``body`` as an ``error.v1`` record, or ``None`` for anything else. Never raises.

    Live, every non-200 the server sends carries one — ``scope_resolution_failed``
    on a 401, ``dependency_unavailable`` with "has no completed build" on a 503
    — and the sentence in ``message`` is the part that says what to fix. A body
    that is not one (a proxy's HTML, a bare status) is simply not read.
    """
    try:
        raw = json.loads(body)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return ErrorRecord.model_validate(raw)
    except ValidationError:
        return None


def http_failure(status: int, body: str) -> tuple[str, tuple[str, ...]]:
    """The detail and the codes an ``http_error`` outcome records for a non-200.

    ``status 503`` alone loses the server's own sentence; with an ``error.v1``
    body it becomes ``status 503 dependency_unavailable: run … has no completed
    build``, clipped, and the code rides on the row's ``error_codes`` where the
    catalog stays the server's. Nothing here branches on ``retryable`` (server
    issue #117: it is not always true) — the client never retries anyway.
    """
    error = parse_error(body)
    if error is None:
        return f"status {status}", ()
    return clip(f"status {status} {error.code}: {error.message}"), (error.code,)
